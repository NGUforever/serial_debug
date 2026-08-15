"""ui/qt_parser_inference.py 解析逻辑 smoke 测试。

覆盖 detect_language / _parse_printf_format / _parse_brace_format /
extract_print_statements / generate_parse_line 五个公共函数。
"""
from __future__ import annotations

import importlib
import sys

import pytest


# qt_parser_inference 不依赖 PySide6，可独立 import
@pytest.fixture
def parser_mod():
    from serial_debug.ui import qt_parser_inference as mod
    return mod


# ---- detect_language ----

@pytest.mark.parametrize('code,expected', [
    ('#include <stdio.h>\nint main(){printf("hi");}', 'c'),
    ('#include <stdio.h>\nLOG_INFO("hello");', 'c'),
    ('std::cout << "x" << std::endl;', 'cpp'),
    ('using namespace std;', 'cpp'),
    ('def foo():\n    print("hi")', 'python'),
    ('print(f"value={x}")', 'python'),
    ('public class A { public static void main(String[] a){} }', 'java'),
    ('fn main() { println!("hi"); }', 'rust'),
    ('console.log("x");', 'js'),
    ('const f = () => 1;', 'js'),
    ('<!DOCTYPE html><html>', 'html'),
    ('', 'python'),
])
def test_detect_language(parser_mod, code, expected):
    assert parser_mod.detect_language(code) == expected


# ---- _parse_printf_format ----

def test_parse_printf_format_basic(parser_mod):
    specs = parser_mod._parse_printf_format('%d %s %.2f')
    assert len(specs) == 3
    assert specs[0].kind == 'int'
    assert specs[1].kind == 'str'
    assert specs[2].kind == 'float'
    assert all(s.name is None for s in specs)


def test_parse_printf_format_escapes_percent(parser_mod):
    specs = parser_mod._parse_printf_format('100%% done %d')
    assert len(specs) == 1
    assert specs[0].kind == 'int'


def test_parse_printf_format_empty(parser_mod):
    assert parser_mod._parse_printf_format('') == []
    assert parser_mod._parse_printf_format('no format here') == []


# ---- _parse_brace_format ----

def test_parse_brace_format_python_fstring(parser_mod):
    specs = parser_mod._parse_brace_format('{}')
    assert len(specs) == 1

    specs = parser_mod._parse_brace_format('{name}')
    assert len(specs) == 1
    assert specs[0].name == 'name'


# ---- extract_print_statements ----

def test_extract_c_log_macros(parser_mod):
    """LOG_TS(level, "MODULE", "fmt", args...) 是项目自定义日志宏格式。
    LOG_INFO("motor rpm=%d", rpm) 这种简单格式由 printf 路径覆盖，不走 LOG_TS 路径。
    """
    code = 'LOG_TS(g_level, "MOTOR", "rpm=%d speed=%.2f", rpm, speed);\n'
    stmts = parser_mod._extract_c_log_macros(code)
    assert len(stmts) == 1
    s = stmts[0]
    # format 应含 ts+level 占位 + MODULE + 用户 fmt
    assert 'MOTOR' in s.format_str
    assert 'rpm=' in s.format_str
    # args 含 ts + level_var + 原 args
    assert 'ts' in s.args
    assert 'g_level' in s.args


def test_extract_c_printf(parser_mod):
    """普通 printf 走 _extract_c_cpp 中的 printf 路径。"""
    code = 'printf("motor rpm=%d speed=%.2f\\n", rpm, speed);\n'
    stmts = parser_mod.extract_print_statements(code, 'c')
    assert len(stmts) == 1
    assert 'motor rpm' in stmts[0].format_str
    assert len(stmts[0].args) == 2


def test_extract_python_fstring(parser_mod):
    code = 'print(f"motor rpm={rpm} speed={speed:.2f}")\n'
    stmts = parser_mod.extract_print_statements(code, 'python')
    assert len(stmts) == 1
    fmt = stmts[0].format_str
    assert 'motor rpm' in fmt


def test_extract_unsupported_lang_returns_empty(parser_mod):
    stmts = parser_mod.extract_print_statements('<html></html>', 'html')
    assert stmts == []


# ---- generate_parse_line ----

def test_generate_parse_line_emits_callable(parser_mod):
    """有 LOG_TS 宏推导出的 stmts，generate_parse_line 应产出可执行 parse_line。"""
    code = 'LOG_TS(g_level, "MOTOR", "rpm=%d", rpm);\n'
    stmts = parser_mod.extract_print_statements(code, 'c')
    assert len(stmts) == 1
    src = parser_mod.generate_parse_line(stmts)
    assert 'def parse_line' in src
    assert 'yield' in src

    ns: dict = {}
    exec(src, ns)
    parse_line = ns['parse_line']
    # 输出格式：<ts> <level> MOTOR rpm=<val>
    results = list(parse_line('12345 F MOTOR rpm=450'))
    assert len(results) > 0
    first = results[0]
    assert len(first) == 3


def test_generate_parse_line_handles_no_match(parser_mod):
    """parse_line 对不匹配的行应静默 yield 0 条。"""
    code = 'LOG_TS(g_level, "MOTOR", "rpm=%d", rpm);\n'
    stmts = parser_mod.extract_print_statements(code, 'c')
    src = parser_mod.generate_parse_line(stmts)
    ns: dict = {}
    exec(src, ns)
    parse_line = ns['parse_line']
    # 不匹配行：迭代结果应为空（generate 会在 parse_line 末尾 `return` 或 `if not m: return`）
    results = list(parse_line('completely unrelated line'))
    assert results == []


def test_generate_parse_line_empty_stmts(parser_mod):
    """stmts 为空时，generate_parse_line 应产出可 import 的占位 parse_line。"""
    src = parser_mod.generate_parse_line([])
    assert 'def parse_line' in src
    ns: dict = {}
    exec(src, ns)
    # 占位 parse_line 调用应不报错（可能 return None / 不 yield）
    assert callable(ns['parse_line'])


# ---- _extract_java (M4 补测) -----------------------------------

def test_extract_java_printf(parser_mod):
    """Java System.out.printf("...", args) 正常解析 + %n 转 \\n。"""
    code = 'System.out.printf("rpm=%d speed=%.2f%n", rpm, speed);\n'
    stmts = parser_mod._extract_java(code)
    assert len(stmts) == 1
    s = stmts[0]
    assert s.language == 'java'
    assert s.style == 'printf'
    assert 'rpm=' in s.format_str
    assert '\n' in s.format_str  # %n 已替换为 \n
    assert len(s.args) == 2
    assert 'rpm' in s.args and 'speed' in s.args


def test_extract_java_println_concat(parser_mod):
    """Java System.out.println("a" + x) 字符串拼接解析（regex 支持 literal+id+literal 链）。"""
    # 当前 regex 要求首 token 为字符串字面量 + 标识符，后续可选 (+ "literal")
    code = 'System.out.println("rpm=" + rpm + " unit");\n'
    stmts = parser_mod._extract_java(code)
    assert len(stmts) == 1
    s = stmts[0]
    assert s.style == 'concat'
    assert 'rpm=' in s.format_str
    assert 'unit' in s.format_str
    assert 'rpm' in s.args


def test_extract_java_no_match(parser_mod):
    """非 Java printf 代码不应误识别。"""
    code = 'int x = 1;\nSystem.out.println();\n'
    stmts = parser_mod._extract_java(code)
    # println() 空参不匹配 println(concat) 正则 → 空
    assert stmts == []


# ---- _extract_rust (M4 补测) ------------------------------------

def test_extract_rust_println(parser_mod):
    """Rust println!("...", args) 正常解析。"""
    code = 'println!("rpm={} speed={:.2}", rpm, speed);\n'
    stmts = parser_mod._extract_rust(code)
    assert len(stmts) == 1
    s = stmts[0]
    assert s.language == 'rust'
    assert s.style == 'format'
    assert 'rpm=' in s.format_str
    assert 'speed=' in s.format_str
    assert len(s.args) == 2


def test_extract_rust_named_args(parser_mod):
    """Rust println!("rpm={rpm}") 命名参数解析。"""
    code = 'println!("rpm={rpm}");\n'
    stmts = parser_mod._extract_rust(code)
    assert len(stmts) == 1
    s = stmts[0]
    assert 'rpm' in s.args


def test_extract_rust_no_match(parser_mod):
    """无 println! 调用 → 空。"""
    code = 'let x = 1;\n'
    stmts = parser_mod._extract_rust(code)
    assert stmts == []


# ---- _extract_js (M4 补测) -------------------------------------

def test_extract_js_template_literal(parser_mod):
    """JS console.log(`...${name}...`) 模板字面量解析。"""
    code = 'console.log(`rpm=${rpm} speed=${speed}`);\n'
    stmts = parser_mod._extract_js(code)
    assert len(stmts) == 1
    s = stmts[0]
    assert s.language == 'js'
    assert s.style == 'template'
    assert 'rpm' in s.args and 'speed' in s.args


def test_extract_js_printf_style(parser_mod):
    """JS console.log("...", args) printf-style 解析。"""
    code = 'console.log("rpm=%d speed=%.2f", rpm, speed);\n'
    stmts = parser_mod._extract_js(code)
    assert len(stmts) == 1
    s = stmts[0]
    assert s.style == 'printf'
    assert len(s.args) == 2


def test_extract_js_no_match(parser_mod):
    """无 console.log → 空。"""
    code = 'const f = () => 1;\n'
    stmts = parser_mod._extract_js(code)
    assert stmts == []


# ---- 各语言 extract via extract_print_statements 集成 ----

def test_extract_print_statements_java(parser_mod):
    """extract_print_statements 按 'java' 调度 _extract_java。"""
    code = 'System.out.printf("v=%d%n", v);\n'
    stmts = parser_mod.extract_print_statements(code, 'java')
    assert len(stmts) == 1
    assert stmts[0].language == 'java'


def test_extract_print_statements_rust(parser_mod):
    """extract_print_statements 按 'rust' 调度 _extract_rust。"""
    code = 'println!("v={}", v);\n'
    stmts = parser_mod.extract_print_statements(code, 'rust')
    assert len(stmts) == 1
    assert stmts[0].language == 'rust'


def test_extract_print_statements_js(parser_mod):
    """extract_print_statements 按 'js' 调度 _extract_js。"""
    code = 'console.log(`v=${v}`);\n'
    stmts = parser_mod.extract_print_statements(code, 'js')
    assert len(stmts) == 1
    assert stmts[0].language == 'js'
