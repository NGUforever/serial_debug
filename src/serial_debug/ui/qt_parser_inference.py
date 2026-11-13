"""从设备/固件源码自动推导日志格式 → 生成 Python parse_line()。

支持语言：C / C++ / Python / Java / Rust / HTML / JS（HTML 仅对照参考，
不参与推导）。每语言一个 extractor（regex + 简单括号配对，不做完整 AST），
返回统一的 `PrintStmt` 列表，由 `generate_parse_line` 合成可执行的 Python
parse_line 函数。

契约：
  - 输入：用户在"解析器配置"dialog 贴入的源码文本
  - 输出：Python 源码字符串，含 `def parse_line(line):` 函数
    yield `(ts, name, value)` 三元组（ts 单位：秒）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Specifier:
    """格式串中的一个占位符。"""
    raw: str           # 原始 token：%d / {} / {name} / {:fmt}
    name: str | None   # 变量名（可能为 None，靠 args 位置映射）
    kind: str          # 'int' / 'float' / 'str' / 'auto'


@dataclass
class PrintStmt:
    """一条 print 语句的解析结果，与语言无关。"""
    raw: str                                  # 原始语句文本
    format_str: str                           # 解析后的格式串（已剥引号 + 解转义）
    args: list[str]                           # 变量名列表（位置对应 specifier）
    style: str                                # printf / fstring / format / concat / template
    specifiers: list[Specifier] = field(default_factory=list)
    language: str = ''
    skip_names: list[str] = field(default_factory=list)
    # skip_names：日志前缀里的辅助变量（如级别 level），生成 parse_line 时
    # 要跳过、不当成数据字段产出。


# ---- 语言识别 ------------------------------------------------------

def detect_language(code: str) -> str:
    """启发式判断源码语言。返回 'c' / 'cpp' / 'python' / 'java' /
    'rust' / 'html' / 'js'；无法判定时兜底 'python'。
    """
    if not code:
        return 'python'
    s = code
    # HTML：含 <html / <!DOCTYPE / 大量 <tag>
    if re.search(r'<!DOCTYPE|<html|<head|<body', s, re.IGNORECASE):
        return 'html'
    # Rust：fn / println! / let mut / pub fn
    if re.search(r'\bfn\s+\w+\s*\(|\bprintln!|\beprintln!|\blet\s+mut\b|\bpub\s+fn\b', s):
        return 'rust'
    # Java：public class / System.out. / package
    if re.search(r'\bpublic\s+class\b|\bSystem\.out\.|\bpackage\s+\w+', s):
        return 'java'
    # C++：std:: / cout / endl / using namespace
    if re.search(r'\bstd::|\bcout\b|\bendl\b|\busing\s+namespace\b', s):
        return 'cpp'
    # JS：console.log / 模板字面量 `${...}` / function / =>
    # （在 C 之前判断——JS 含 `;` 行尾但不是 C）
    if re.search(r'\bconsole\.log\b|`[^`]*\$\{|\bfunction\s+\w+\s*\(|=>|\bconst\s+\w+\s*=', s):
        return 'js'
    # Python：def / import / print( / f"...{...}"
    if re.search(r'\bdef\s+\w+\s*\(|\bimport\s+\w+|\bprint\s*\(|\bf"[^"]*\{', s):
        return 'python'
    # C 明确标志（在通用启发式之前判断）
    c_markers = [
        r'#include\s*[<"]',
        r'#define\s+\w+',
        r'\btypedef\b',
        r'\bstruct\s+\w+\s*\{',
        r'\bLOG_[A-Z_]+\s*\(',
        r'\bprintf\s*\(',
        r'\bfprintf\s*\(',
        r'\bsprintf\s*\(',
        r'\bsnprintf\s*\(',
        r'\bstatic\s+(?:inline\s+)?(?:void|int|char|float|double)\b',
        r'\b(?:void|int|char|float|double|uint\d+_t|int\d+_t)\s+\w+\s*\(',
    ]
    if any(re.search(p, s) for p in c_markers):
        return 'c'
    # 兜底：含 ; 结尾的多行 + 没有任何其他语言关键字 → 当 C
    semicolons = len(re.findall(r';\s*$', s, re.MULTILINE))
    nonblank_lines = sum(1 for ln in s.splitlines() if ln.strip())
    if nonblank_lines > 0 and semicolons / nonblank_lines > 0.3:
        return 'c'
    return 'python'


# ---- 各语言 extractor ---------------------------------------------

def _parse_printf_format(fmt: str) -> list[Specifier]:
    """解析 printf-style 格式串（C / Java printf / Python %-style）。

    识别 %d / %ld / %i / %u / %f / %lf / %.2f / %-5s / %c / %s / %x
    等常见变体。返回 Specifier 列表（name=None，靠位置映射）。
    """
    specs: list[Specifier] = []
    # 跳过 %%（字面百分号）
    i = 0
    while i < len(fmt):
        if fmt[i] == '%':
            if i + 1 < len(fmt) and fmt[i + 1] == '%':
                i += 2
                continue
            # 匹配 % 后直到转换字符
            m = re.match(r'%([-+ #0]*)(\d+)?(?:\.(\d+))?(l|ll|h|hh)?([diufsexXc])',
                         fmt[i:])
            if m:
                conv = m.group(5)
                if conv in ('d', 'i', 'u', 'x', 'X', 'c'):
                    kind = 'int' if conv != 'c' else 'str'
                elif conv in ('f', 'e', 'g', 'E', 'G'):
                    kind = 'float'
                else:
                    kind = 'str'
                specs.append(Specifier(
                    raw=fmt[i:i + m.end()], name=None, kind=kind))
                i += m.end()
                continue
        i += 1
    return specs


def _parse_brace_format(fmt: str) -> list[Specifier]:
    """解析 brace-style 格式串（Python .format / Rust println!）。

    识别 {} / {0} / {name} / {:fmt} / {name:fmt} / {:?}（Rust debug）。
    """
    specs: list[Specifier] = []
    i = 0
    while i < len(fmt):
        if fmt[i] == '{':
            if i + 1 < len(fmt) and fmt[i + 1] == '{':
                i += 2  # 字面 {{
                continue
            end = fmt.find('}', i)
            if end < 0:
                break
            inner = fmt[i + 1:end]
            # 分 name 与 fmt（用 : 切）
            name = None
            if ':' in inner:
                name_part, fmt_part = inner.split(':', 1)
            else:
                name_part, fmt_part = inner, ''
            if name_part:
                name = name_part
            kind = 'auto'
            if fmt_part:
                if '?' in fmt_part or fmt_part.endswith('?'):
                    kind = 'str'  # Rust {:?} debug
                elif 'f' in fmt_part or 'e' in fmt_part or 'g' in fmt_part:
                    kind = 'float'
                elif 'd' in fmt_part:
                    kind = 'int'
            specs.append(Specifier(
                raw=fmt[i:end + 1], name=name, kind=kind))
            i = end + 1
            continue
        i += 1
    return specs


def _strip_type_modifier(arg: str) -> str:
    """C/C++/Java：剥掉类型修饰，只留标识符。

    `int x` → `x`；`const char* name` → `name`；`get_value()` → `get_value()`
    复杂表达式（含 . -> [] ()）保留原样，后续若不是简单标识符就跳过。
    """
    a = arg.strip()
    # 简单情况：直接是标识符
    if re.fullmatch(r'[A-Za-z_]\w*', a):
        return a
    # 取最后一个 word（如 `int x` → `x`）
    m = re.search(r'\b([A-Za-z_]\w*)\s*$', a)
    if m and ' ' in a:
        # 含空格才剥类型；纯 `arr[i]` 不剥
        return m.group(1)
    return a


def _split_args_c(arg_str: str) -> list[str]:
    """C/C++/Java：按顶层逗号切分参数（不进括号），剥类型，保标识符。"""
    out: list[str] = []
    depth = 0
    cur = ''
    for ch in arg_str:
        if ch in '([{':
            depth += 1
            cur += ch
        elif ch in ')]}':
            depth -= 1
            cur += ch
        elif ch == ',' and depth == 0:
            out.append(cur)
            cur = ''
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    # 剥类型 + 拒绝非简单标识符
    result: list[str] = []
    for a in out:
        s = _strip_type_modifier(a)
        if re.fullmatch(r'[A-Za-z_]\w*', s):
            result.append(s)
    return result


def _split_args_c_dotted(arg_str: str) -> list[str]:
    """LOG_TS 宏专用参数切分：允许 dotted / arrow / index 表达式。

    例如 `BMS_Data.state, LK_pwm_set[0], p->vbus` 都能保留为变量名。
    """
    out: list[str] = []
    depth = 0
    cur = ''
    for ch in arg_str:
        if ch in '([{':
            depth += 1
            cur += ch
        elif ch in ')]}':
            depth -= 1
            cur += ch
        elif ch == ',' and depth == 0:
            out.append(cur)
            cur = ''
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    dotted_pat = re.compile(
        r'^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*|->[A-Za-z_]\w*|\[\w+\])*$')
    result: list[str] = []
    for a in out:
        s = _strip_type_modifier(a)
        if dotted_pat.match(s):
            result.append(s)
    return result


# ---- C 封装日志宏模式（可扩展）---------------------------------------
#
# 每个模式描述一类"封装过的异步串口日志宏"，如：
#   LOG_TS(AM_LOG_LEVEL_FATAL, "LIFT", "liftup hall %x -> %x\n", a.x, b.x)
# 实际输出：`1794062959017 F LIFT liftup hall 3a -> 1`
#   <13位ms时间戳> <级别单字母> <TAG> <格式串渲染后的内容>
#
# 模式字段：
#   name        : 模式名（文档用）
#   macro_re    : 宏名 regex（\b 由外层补，这里只写名字部分）
#   has_level   : 第 1 参数是否为级别（宏常量/级别变量，如 AM_LOG_LEVEL_FATAL）
#   has_tag     : level 之后是否有一个 TAG 字符串字面量参数
#                 （ESP_LOGI 的 TAG 参数通常是变量 TAG/名字，同样按 TAG 处理）
# 输出行前缀统一假定为 `<ts> <level字母> [<TAG>] <格式串渲染>`。
# 如何扩展：固件里遇到新的封装宏（如 MY_LOG(tag, fmt, ...)），在此列表加一项
# {'name': 'my_log', 'macro_re': r'MY_LOG', 'has_level': False, 'has_tag': True}
# 即可被识别；无需改 generate_parse_line。
C_LOG_MACRO_PATTERNS = [
    {'name': 'LOG_TS',   'macro_re': r'[A-Z][A-Z0-9_]*TS', 'has_level': True,  'has_tag': True},
    {'name': 'LOG_XXX',  'macro_re': r'LOG_[A-Z0-9_]+',    'has_level': True,  'has_tag': True},
    {'name': 'LOGI/LOGD/...', 'macro_re': r'LOG[IDEWFV]',  'has_level': False, 'has_tag': False},
    {'name': 'ESP_LOGX', 'macro_re': r'ESP_LOG[IDEWFV]',   'has_level': False, 'has_tag': True},
]

# 级别常量 / 级别字母：不当作数据字段
_LEVEL_CONST_RE = re.compile(r'^[A-Z][A-Z0-9_]*LOG_LEVEL_[A-Z0-9_]+$|^LOG_(?:LEVEL_)?[A-Z]+$')
_LEVEL_LETTER_RE = re.compile(r'^[FWEIDVTF]$')


def _macro_pattern_for(macro_name: str) -> dict | None:
    """按宏名查 C_LOG_MACRO_PATTERNS（按列表顺序，先匹配先得）。"""
    for pat in C_LOG_MACRO_PATTERNS:
        if re.fullmatch(pat['macro_re'], macro_name):
            return pat
    return None


def _arg_to_var_name(arg: str) -> str | None:
    """把 C 参数表达式转成 parse_line 变量名。

    规则：取表达式中最后 1~2 个标识符用 `_` 拼接（`pre_state.hall.value`
    → `hall_value`；`p->vbus` → `vbus`；`buf[2]` → `buf`；`rpm` → `rpm`）。
    取两段是为了在嵌套结构体里保留字段语义（`hall.value`→`hall_value`），
    同时剥掉 `.` `->` `[]` 等不合法于 Python 变量名的字符。
    若与 TAG 派生名或已有名冲突由调用方加 `_2` 后缀去重。
    """
    a = arg.strip()
    if not a:
        return None
    ids = re.findall(r'[A-Za-z_]\w*', a)
    if not ids:
        return None
    return '_'.join(ids[-2:])


def _dedupe_name(name: str, used: set[str]) -> str:
    """重名时加 _2 / _3 … 后缀，保证预览表变量名唯一可读。"""
    if name not in used:
        used.add(name)
        return name
    n = 2
    while f'{name}_{n}' in used:
        n += 1
    used.add(f'{name}_{n}')
    return f'{name}_{n}'


def _extract_c_log_macros(code: str) -> list[PrintStmt]:
    """C 封装日志宏：LOG_TS / LOG_XXX / LOGI / ESP_LOGI 等模式。

    识别 C_LOG_MACRO_PATTERNS 里声明的宏调用，假定输出格式为
    `<13位ms时间戳> <级别字母> [<TAG>] <格式串渲染内容>`。

    日志行示例：`1794062959017 F LIFT liftup hall 3a -> 1`
      - 1794062959017 = ts（对时后毫秒时间戳，未对时为 MCU ticks）
      - F = 级别字母（AM_LOG_LEVEL_FATAL→F 等，跳过不产字段）
      - LIFT = TAG 字面量
      - 其余 = 用户格式串展开

    生成 PrintStmt：format_str 拼为 `{} {} [TAG] <user_fmt>`；前两个 brace
    spec 对应 ts（int）/ level（str）。级别与 TAG 段会写入 skip_names，
    generate_parse_line 据此跳过、不产数据字段。可变参数按占位符顺序绑定，
    变量名取参数表达式最后一个标识符（`pre_state.hall.value`→`value`），
    重名加 `_2` 后缀去重。%x/%X 生成 hex 捕获组（支持 `3a` 这类带字母的
    十六进制值）。
    """
    stmts: list[PrintStmt] = []
    # args 可能跨多行（C 行尾 \ 延续），先压平
    flat = re.sub(r'\\\s*\n\s*', ' ', code)
    flat = re.sub(r'\s+', ' ', flat)
    # 同一宏调用 span 只取最先（最具体）命中的模式，防止
    # `LOG_TS` 同时命中 LOG_TS 与 LOG_XXX 两个模式产生重复 stmt
    claimed: set[tuple[int, int]] = set()
    for pat in C_LOG_MACRO_PATTERNS:
        pat_re = re.compile(
            r'\b(' + pat['macro_re'] + r')\s*\(')
        for pm in pat_re.finditer(flat):
            # 从 ( 起做括号配对，取出完整参数串（支持嵌套括号参数）
            start = pm.end()  # 指向 '(' 后
            depth = 1
            i = start
            while i < len(flat) and depth:
                if flat[i] == '(':
                    depth += 1
                elif flat[i] == ')':
                    depth -= 1
                i += 1
            if depth:
                continue  # 括号不配对，放弃
            span = (pm.start(), i)
            if span in claimed:
                continue
            claimed.add(span)
            inner = flat[start:i - 1]
            _parse_macro_call(pm.group(1), inner, pat, stmts)
    return stmts


def _parse_macro_call(macro_name: str, inner: str, pat: dict,
                      stmts: list[PrintStmt]) -> None:
    """解析一个宏调用的参数串 inner，产出 PrintStmt（若有格式串）。"""
    # 顶层逗号切分（尊重括号与字符串内逗号）
    parts: list[str] = []
    depth = 0
    cur = ''
    in_str = False
    i = 0
    while i < len(inner):
        ch = inner[i]
        if in_str:
            cur += ch
            if ch == '\\' and i + 1 < len(inner):
                cur += inner[i + 1]
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            cur += ch
        elif ch in '([':
            depth += 1
            cur += ch
        elif ch in ')]':
            depth -= 1
            cur += ch
        elif ch == ',' and depth == 0:
            parts.append(cur)
            cur = ''
        else:
            cur += ch
        i += 1
    if cur.strip():
        parts.append(cur)

    # 找第一个字符串字面量参数：按模式不同，它可能是 fmt 或 TAG
    # has_level 时第 0 参数是级别表达式（宏常量/变量）
    str_idx = [j for j, p in enumerate(parts) if re.match(r'^\s*"', p)]
    if not str_idx:
        return  # 无字符串字面量，无法推导格式串
    fmt_idx = str_idx[-1] if pat['has_tag'] else str_idx[0]
    fmt = _unescape_c_string(re.match(r'^\s*"((?:[^"\\]|\\.)*)"', parts[fmt_idx]).group(1))
    args_strs = parts[fmt_idx + 1:]

    module = ''
    if pat['has_tag'] and len(str_idx) >= 2:
        # TAG 是 fmt 前一个字符串字面量
        tag_idx = fmt_idx - 1
        module = _unescape_c_string(re.match(r'^\s*"((?:[^"\\]|\\.)*)"', parts[tag_idx]).group(1))

    # 级别参数：has_level 时是第 0 参数（宏常量如 AM_LOG_LEVEL_FATAL）
    level_expr = parts[0].strip() if pat['has_level'] else ''
    # 级别名：宏常量取常量名本身，用于 skip；普通变量同理
    level_name = level_expr if level_expr else ''

    specs = _parse_printf_format(fmt)
    # 参数绑定：每个 specifier 对应一个 arg，变量名取最后一个标识符
    var_names: list[str] = []
    used = set()
    for j, sp in enumerate(specs):
        if j < len(args_strs):
            nm = _arg_to_var_name(args_strs[j])
        else:
            nm = None
        if not nm or _LEVEL_CONST_RE.match(nm):
            nm = f'arg{j}'
        var_names.append(_dedupe_name(nm, used))

    # 前缀：ts + 级别字母（+ TAG）
    prefix = '{} {} '
    full_specs = [
        Specifier(raw='{}', name='ts', kind='int'),
        Specifier(raw='{}', name='level', kind='str'),
    ]
    full_args = ['ts', 'level']
    skip = ['level']
    if level_name:
        skip.append(level_name)
    if module:
        prefix += module + ' '
    full_fmt = prefix + fmt.rstrip()
    # args/specs 与 full_* 拼接（TAG 是字面量拼进 fmt，无 spec）
    full_args += var_names
    full_specs += specs
    # %x/%X 的 hex 支持：改 kind → 'hex'（_specifier_to_regex 会处理）
    for sp in specs:
        if re.search(r'[xX]\s*$', sp.raw.strip()):
            sp.kind = 'hex'
    stmts.append(PrintStmt(
        raw=macro_name + '(...)',
        format_str=full_fmt,
        args=full_args,
        style='log_macro',
        specifiers=full_specs,
        language='c',
        skip_names=skip,
    ))


def _extract_c_cpp(code: str) -> list[PrintStmt]:
    """C / C++：printf / fprintf / sprintf / snprintf + std::cout << ...
    + LOG_<NAME>(...) 自定义日志宏。"""
    stmts: list[PrintStmt] = []
    lang = 'c'
    if re.search(r'\bstd::|\bcout\b', code):
        lang = 'cpp'
    # printf 家族
    pat = re.compile(
        r'(?:std::)?(?:fprintf|printf|sprintf|snprintf)\s*\(\s*'
        r'(?:"((?:[^"\\]|\\.)*)")\s*,\s*([^)]*)\)')
    for m in pat.finditer(code):
        raw = m.group(0)
        fmt = _unescape_c_string(m.group(1))
        args = _split_args_c(m.group(2))
        specs = _parse_printf_format(fmt)
        stmts.append(PrintStmt(
            raw=raw, format_str=fmt, args=args,
            style='printf', specifiers=specs, language=lang))
    # C++ std::cout << ... << ... 流式
    cout_pat = re.compile(r'std::cout\s*((?:<<[^;]+)+)')
    for m in cout_pat.finditer(code):
        raw = m.group(0)
        body = m.group(1)
        # 拆 << 后的 token
        toks = [t.strip() for t in body.split('<<') if t.strip()]
        # 把字面量 "..." 与变量交替
        fmt_parts: list[str] = []
        args: list[str] = []
        specs: list[Specifier] = []
        for tok in toks:
            sm = re.match(r'^"((?:[^"\\]|\\.)*)"$', tok)
            if sm:
                # 字面量
                lit = _unescape_c_string(sm.group(1))
                fmt_parts.append(lit)
            elif tok in ('std::endl', 'std::flush', 'endl', 'flush', '"\\n"', '"\n"'):
                fmt_parts.append('\n')
            else:
                # 视为变量
                var = _strip_type_modifier(tok)
                if re.fullmatch(r'[A-Za-z_]\w*', var):
                    args.append(var)
                    specs.append(Specifier(raw='{}', name=var, kind='auto'))
                    fmt_parts.append('{}')
        # 合并 format_str
        fmt = ''.join(fmt_parts)
        if args:
            stmts.append(PrintStmt(
                raw=raw, format_str=fmt, args=args,
                style='concat', specifiers=specs, language=lang))
    # LOG_<NAME>(...) 自定义日志宏
    stmts.extend(_extract_c_log_macros(code))
    return stmts


def _extract_python(code: str) -> list[PrintStmt]:
    """Python：f-string / .format() / %-style / logging."""
    stmts: list[PrintStmt] = []
    # f-string：print(f"...") 或 logging.info(f"...")
    pat_fs = re.compile(
        r'\b(?:print|logging\.\w+|log\.\w+)\s*\(\s*'
        r'f"((?:[^"\\]|\\.)*)"\s*\)')
    for m in pat_fs.finditer(code):
        fmt = _unescape_py_string(m.group(1))
        specs = _parse_brace_format(fmt)
        # args 来自 specifiers 的 name（内联）
        args = [s.name or f'arg{i}' for i, s in enumerate(specs)]
        stmts.append(PrintStmt(
            raw=m.group(0), format_str=fmt, args=args,
            style='fstring', specifiers=specs, language='python'))
    # .format()：print("...".format(a, b))
    pat_fmt = re.compile(
        r'\b(?:print|logging\.\w+)\s*\(\s*'
        r'"((?:[^"\\]|\\.)*)"\.format\s*\(\s*([^)]*)\s*\)\s*\)')
    for m in pat_fmt.finditer(code):
        fmt = _unescape_py_string(m.group(1))
        specs = _parse_brace_format(fmt)
        args = [a.strip() for a in m.group(2).split(',') if a.strip()]
        # 简化：只保留简单标识符
        args = [a for a in args if re.fullmatch(r'[A-Za-z_]\w*', a)]
        # 若 specifiers 都是位置 {}，args 顺序对应
        # 若 specifiers 有 name，name 优先
        final_args: list[str] = []
        for i, s in enumerate(specs):
            if s.name:
                final_args.append(s.name)
            elif i < len(args):
                final_args.append(args[i])
            else:
                final_args.append(f'arg{i}')
        stmts.append(PrintStmt(
            raw=m.group(0), format_str=fmt, args=final_args,
            style='format', specifiers=specs, language='python'))
    # %-style：print("..." % (a, b)) 或 logging.info("...", a, b)
    pat_pct = re.compile(
        r'\b(?:print|logging\.\w+)\s*\(\s*'
        r'"((?:[^"\\]|\\.)*)"\s*%\s*\(\s*([^)]*)\s*\)\s*\)')
    for m in pat_pct.finditer(code):
        fmt = _unescape_py_string(m.group(1))
        specs = _parse_printf_format(fmt)
        args = [a.strip() for a in m.group(2).split(',') if a.strip()]
        args = [a for a in args if re.fullmatch(r'[A-Za-z_]\w*', a)]
        final_args: list[str] = []
        for i, _ in enumerate(specs):
            if i < len(args):
                final_args.append(args[i])
            else:
                final_args.append(f'arg{i}')
        stmts.append(PrintStmt(
            raw=m.group(0), format_str=fmt, args=final_args,
            style='printf', specifiers=specs, language='python'))
    return stmts


def _extract_java(code: str) -> list[PrintStmt]:
    """Java：System.out.printf / println / print + String concat."""
    stmts: list[PrintStmt] = []
    # System.out.printf("...", args)
    pat = re.compile(
        r'System\.out\.printf\s*\(\s*'
        r'"((?:[^"\\]|\\.)*)"\s*,\s*([^)]*)\)')
    for m in pat.finditer(code):
        fmt = _unescape_c_string(m.group(1))
        # Java 用 %n 表示换行 → 转 \n
        fmt = fmt.replace('%n', '\n')
        args = _split_args_c(m.group(2))
        specs = _parse_printf_format(fmt)
        stmts.append(PrintStmt(
            raw=m.group(0), format_str=fmt, args=args,
            style='printf', specifiers=specs, language='java'))
    # System.out.println("a" + x + "b" + y) / print(...)
    pat2 = re.compile(
        r'System\.out\.println\s*\(\s*'
        r'((?:"(?:[^"\\]|\\.)*"\s*\+\s*[A-Za-z_]\w*(?:\s*\+\s*"(?:[^"\\]|\\.)*")*)+)\s*\)')
    for m in pat2.finditer(code):
        raw = m.group(0)
        body = m.group(1)
        # 拆字面量与变量
        toks = [t.strip() for t in body.split('+') if t.strip()]
        fmt_parts: list[str] = []
        args: list[str] = []
        specs: list[Specifier] = []
        for tok in toks:
            sm = re.match(r'^"((?:[^"\\]|\\.)*)"$', tok)
            if sm:
                fmt_parts.append(_unescape_c_string(sm.group(1)))
            else:
                if re.fullmatch(r'[A-Za-z_]\w*', tok):
                    args.append(tok)
                    specs.append(Specifier(raw='{}', name=tok, kind='auto'))
                    fmt_parts.append('{}')
        fmt = ''.join(fmt_parts) + '\n'
        if args:
            stmts.append(PrintStmt(
                raw=raw, format_str=fmt, args=args,
                style='concat', specifiers=specs, language='java'))
    return stmts


def _extract_rust(code: str) -> list[PrintStmt]:
    """Rust：println! / print! / eprintln! 宏。"""
    stmts: list[PrintStmt] = []
    pat = re.compile(
        r'\b(?:println|print|eprintln|eprint|writeln|write)\s*!\s*\(\s*'
        r'"((?:[^"\\]|\\.)*)"\s*(?:,\s*([^)]*))?\s*\)')
    for m in pat.finditer(code):
        fmt = _unescape_rust_string(m.group(1))
        specs = _parse_brace_format(fmt)
        args_str = m.group(2) or ''
        args = [a.strip() for a in args_str.split(',') if a.strip()]
        args = [a for a in args if re.fullmatch(r'[A-Za-z_]\w*', a)]
        final_args: list[str] = []
        for i, s in enumerate(specs):
            if s.name:
                final_args.append(s.name)
            elif i < len(args):
                final_args.append(args[i])
            else:
                final_args.append(f'arg{i}')
        stmts.append(PrintStmt(
            raw=m.group(0), format_str=fmt, args=final_args,
            style='format', specifiers=specs, language='rust'))
    return stmts


def _extract_js(code: str) -> list[PrintStmt]:
    """JS：console.log + 模板字面量 + 字符串拼接。"""
    stmts: list[PrintStmt] = []
    # 模板字面量：`...${name}...`
    pat_tmpl = re.compile(r'console\.log\s*\(\s*`([^`]*)`\s*\)')
    for m in pat_tmpl.finditer(code):
        fmt = m.group(1)
        specs: list[Specifier] = []
        i = 0
        new_fmt = ''
        while i < len(fmt):
            if fmt[i] == '$' and i + 1 < len(fmt) and fmt[i + 1] == '{':
                end = fmt.find('}', i)
                if end < 0:
                    break
                name = fmt[i + 2:end].strip()
                # 简化：取标识符
                m_id = re.match(r'^[A-Za-z_]\w*', name)
                if m_id:
                    name = m_id.group(0)
                specs.append(Specifier(raw='${' + name + '}',
                                       name=name, kind='auto'))
                new_fmt += '{}'
                i = end + 1
                continue
            new_fmt += fmt[i]
            i += 1
        args = [s.name or f'arg{i}' for i, s in enumerate(specs)]
        if specs:
            stmts.append(PrintStmt(
                raw=m.group(0), format_str=new_fmt, args=args,
                style='template', specifiers=specs, language='js'))
    # printf-style：console.log("ts=%d ...", ts, ...)
    pat_pct = re.compile(
        r'console\.log\s*\(\s*"((?:[^"\\]|\\.)*)"\s*,\s*([^)]*)\)')
    for m in pat_pct.finditer(code):
        fmt = _unescape_c_string(m.group(1))
        specs = _parse_printf_format(fmt)
        args = [a.strip() for a in m.group(2).split(',') if a.strip()]
        args = [a for a in args if re.fullmatch(r'[A-Za-z_]\w*', a)]
        final_args = []
        for i, _ in enumerate(specs):
            if i < len(args):
                final_args.append(args[i])
            else:
                final_args.append(f'arg{i}')
        stmts.append(PrintStmt(
            raw=m.group(0), format_str=fmt, args=final_args,
            style='printf', specifiers=specs, language='js'))
    return stmts


def extract_print_statements(code: str, lang: str) -> list[PrintStmt]:
    """按语言调度对应 extractor。返回 PrintStmt 列表。"""
    if not code:
        return []
    if lang in ('c', 'cpp'):
        return _extract_c_cpp(code)
    if lang == 'python':
        return _extract_python(code)
    if lang == 'java':
        return _extract_java(code)
    if lang == 'rust':
        return _extract_rust(code)
    if lang == 'js':
        return _extract_js(code)
    # html 不产生日志
    return []


# ---- 字符串转义解析 ----------------------------------------------

def _unescape_c_string(s: str) -> str:
    """把 C/Java 字符串字面量里的 \\n \\t \\" \\\\ 等还原。"""
    out = []
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s):
            nx = s[i + 1]
            mp = {'n': '\n', 't': '\t', 'r': '\r', '"': '"',
                  '\\': '\\', '0': '\0', 'a': '\a', 'b': '\b',
                  'f': '\f', 'v': '\v'}
            if nx in mp:
                out.append(mp[nx])
                i += 2
                continue
        out.append(s[i])
        i += 1
    return ''.join(out)


def _unescape_py_string(s: str) -> str:
    """Python 字符串字面量转义还原（简化版，复用 C 表）。"""
    return _unescape_c_string(s)


def _unescape_rust_string(s: str) -> str:
    """Rust 字符串字面量转义还原（同 C 表 + \\u{...}）"""
    return _unescape_c_string(s)


# ---- 解析器生成 --------------------------------------------------

def _specifier_to_regex(spec: Specifier, var_name: str) -> str:
    """把一个 specifier 转成 regex 捕获组。"""
    if spec.kind == 'hex':
        # %x：十六进制（可带 0x 前缀），日志里可能是 `3a` / `0x3A`
        return r'(0[xX][0-9a-fA-F]+|[0-9a-fA-F]+)'
    if spec.kind == 'int':
        return r'(-?\d+)'
    if spec.kind == 'float':
        return r'(-?[\d.]+(?:[eE][-+]?\d+)?)'
    if spec.kind == 'str':
        return r'(\S+)'
    # auto：兼容 int / float / 字符串
    return r'(-?[\d.]+(?:[eE][-+]?\d+)?|\S+)'


def _format_to_regex(fmt: str, specs: list[Specifier],
                     var_names: list[str]) -> str:
    """把格式串 + specifiers 合成可执行的 regex。

    把每个 specifier 替换为捕获组；字面量部分用 re.escape。
    """
    out: list[str] = []
    spec_idx = 0
    i = 0
    while i < len(fmt):
        if fmt[i] == '%':
            if i + 1 < len(fmt) and fmt[i + 1] == '%':
                out.append(r'%')
                i += 2
                continue
            m = re.match(r'%[-+ #0]*(?:\d+)?(?:\.\d+)?(?:l|ll|h|hh)?[diufsexXc]',
                         fmt[i:])
            if m and spec_idx < len(specs):
                grp = _specifier_to_regex(specs[spec_idx],
                                          var_names[spec_idx] if spec_idx < len(var_names) else f'arg{spec_idx}')
                out.append(grp)
                spec_idx += 1
                i += m.end()
                continue
            # 未识别的 % → 当字面
            out.append(re.escape('%'))
            i += 1
            continue
        if fmt[i] == '{':
            if i + 1 < len(fmt) and fmt[i + 1] == '{':
                out.append(re.escape('{'))
                i += 2
                continue
            end = fmt.find('}', i)
            if end < 0:
                out.append(re.escape(fmt[i:]))
                break
            if spec_idx < len(specs):
                var = (var_names[spec_idx]
                       if spec_idx < len(var_names) else f'arg{spec_idx}')
                out.append(_specifier_to_regex(specs[spec_idx], var))
                spec_idx += 1
            else:
                out.append(r'(\S+)')
            i = end + 1
            continue
        # 字面量字符
        out.append(re.escape(fmt[i]))
        i += 1
    return ''.join(out)


_TS_NAMES = {'ts', 'time', 't', 'timestamp', 'millis', 'ms'}


def generate_parse_line(stmts: list[PrintStmt]) -> str:
    """把 PrintStmt 列表合成 Python parse_line 函数源码。"""
    if not stmts:
        return ('# 未能从源码推导出 print 语句。\n'
                '# 请检查源码或手动编写 parse_line。\n'
                'def parse_line(line):\n    return\n')
    # 去 trailing 空白（避免 re.escape('\n') 留下尾部 \\ 字符）
    for st in stmts:
        st.format_str = st.format_str.rstrip()
    lines = [
        '# 自动推导自源码——可手动微调。',
        '# 推导规则：',
    ]
    for i, st in enumerate(stmts):
        lines.append(f'#   {i + 1}. [{st.language}/{st.style}] {st.raw[:80]}')
    lines.append('import re')
    # 为每条 stmt 生成一个 regex
    var_lists: list[list[str]] = []
    for i, st in enumerate(stmts):
        var_names = list(st.args) if st.args else (
            [f'arg{j}' for j in range(len(st.specifiers))])
        # 若 specifiers 有 name，优先用 name
        for j, spec in enumerate(st.specifiers):
            if spec.name and (j >= len(var_names) or var_names[j] == f'arg{j}'):
                if j < len(var_names):
                    var_names[j] = spec.name
                else:
                    var_names.append(spec.name)
        regex = _format_to_regex(st.format_str, st.specifiers, var_names)
        lines.append(f'_re_{i + 1} = re.compile({regex!r})')
        var_lists.append(var_names)
    lines.append('')
    lines.append('def parse_line(line):')
    lines.append('    # 多条 print 语句任一匹配即产出')
    lines.append('    for _re, _vars, _skip in (')
    for i, vl in enumerate(var_lists):
        # skip_names 必须取本条 stmt 的——st 是上循环残留值，
        # 会让所有 regex 行共用最后一条 stmt 的 skip 列表（复审 Major 2）
        lines.append(f'        (_re_{i + 1}, {vl!r}, {stmts[i].skip_names!r}),')
    lines.append('    ):')
    lines.append('        m = _re.search(line)')
    lines.append('        if not m:')
    lines.append('            continue')
    lines.append('        parts = m.groups()')
    lines.append('        # 时间戳：若 _vars 含 ts/time/t 则用之；否则用行首 13 位毫秒')
    lines.append('        ts = None')
    lines.append('        for i, name in enumerate(_vars):')
    lines.append('            if name in (\'ts\', \'time\', \'t\', \'timestamp\', '
                 '\'millis\', \'ms\') and i < len(parts):')
    lines.append('                try:')
    lines.append('                    ts = float(parts[i])')
    lines.append('                    if ts > 1e11:  # 毫秒时间戳')
    lines.append('                        ts = ts / 1000.0')
    lines.append('                except (ValueError, IndexError):')
    lines.append('                    pass')
    lines.append('                break')
    lines.append('        if ts is None:')
    lines.append('            # 用行首 13 位毫秒 / 1000')
    lines.append('            # 说明：ts 为 unix 秒——由行首 13 位毫秒时间戳自动')
    lines.append('            # 映射（/1000），曲线 X 轴会格式化为 HH:MM:SS 时间显示。')
    lines.append('            import re as _re2')
    lines.append('            _m = _re2.match(r\'(\\d{10,13})\', line)')
    lines.append('            if _m:')
    lines.append('                ts = int(_m.group(1)) / 1000.0')
    lines.append('            else:')
    lines.append('                return  # 无时间戳，跳过')
    lines.append('        for i, name in enumerate(_vars):')
    lines.append('            if name in (\'ts\', \'time\', \'t\', \'timestamp\', '
                 '\'millis\', \'ms\'):')
    lines.append('                continue')
    lines.append('            if i >= len(parts):')
    lines.append('                continue')
    lines.append('            if name in _skip:  # 前缀辅助段（级别字母等），非数据字段')
    lines.append('                continue')
    lines.append('            try:')
    lines.append('                yield ts, name, float(parts[i])')
    lines.append('            except (ValueError, IndexError):')
    lines.append('                try:  # %x 十六进制值（如 3a）')
    lines.append('                    yield ts, name, float(int(parts[i], 16))')
    lines.append('                except (ValueError, IndexError):')
    lines.append('                    pass')
    lines.append('    return')
    return '\n'.join(lines) + '\n'
