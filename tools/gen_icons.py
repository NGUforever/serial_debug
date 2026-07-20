"""从 SVG 源重新生成 PNG/ICO/ICNS 多分辨率（串口调试 + 日志查看）。

需要 cairosvg 渲染 SVG；不可用时 fallback 到内嵌的 PIL 绘制器
（绘制效果不如 SVG 精致，但保证可用）。
"""
import os
import shutil
from PIL import Image, ImageDraw, ImageFont

# 仓库根在 tools/ 上一层，资源在 src/serial_debug/resources/assets
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
ASSETS = os.path.join(_ROOT, 'src', 'serial_debug', 'resources', 'assets')
SIZES = [16, 24, 32, 48, 64, 128, 256, 512, 1024]


def _try_cairosvg_render(svg_path: str, size: int) -> Image.Image | None:
    try:
        import cairosvg  # type: ignore
    except ImportError:
        return None
    try:
        png_bytes = cairosvg.svg2png(url=svg_path, output_width=size, output_height=size)
        from io import BytesIO
        return Image.open(BytesIO(png_bytes)).convert('RGBA')
    except Exception as e:
        print(f'  cairosvg render 失败 {svg_path}: {e}')
        return None


def _bg_gradient(size: int) -> Image.Image:
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / size
        if t < 0.55:
            k = t / 0.55
            c = (int(0x2a + (0x1e - 0x2a) * k),
                 int(0x2d + (0x1e - 0x2d) * k),
                 int(0x3a + (0x1e - 0x1e) * k), 255)
        else:
            k = (t - 0.55) / 0.45
            c = (int(0x1e + (0x18 - 0x1e) * k),
                 int(0x1e + (0x18 - 0x1e) * k),
                 int(0x1e + (0x18 - 0x1e) * k), 255)
        d.line([(0, y), (size, y)], fill=c)
    # 圆角 mask
    r = int(48 * size / 256)
    mask = Image.new('L', (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, size, size], radius=r, fill=255)
    img.putalpha(mask)
    return img


def _draw_debug(size: int) -> Image.Image:
    img = _bg_gradient(size)
    d = ImageDraw.Draw(img)
    s = size / 256.0

    d.rounded_rectangle([int(48 * s), int(44 * s), int(208 * s), int(49 * s)],
                         radius=int(2.5 * s), fill=(0, 122, 204, 255))

    if size >= 64:
        try:
            font_s = ImageFont.truetype(
                '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf',
                int(11 * s))
            d.text((int(48 * s), int(64 * s)), '[12:34:56.789]',
                   fill=(0x6a, 0x99, 0x55, 255), font=font_s)
            d.text((int(48 * s), int(80 * s)), 'TX:> AT+ELOG=0%',
                   fill=(0xdc, 0xdc, 0xaa, 255), font=font_s)
        except Exception:
            pass

    # USB 外壳
    d.rounded_rectangle([int(44 * s), int(120 * s), int(122 * s), int(178 * s)],
                         radius=int(6 * s), fill=(0xc8, 0xc8, 0xc8, 255),
                         outline=(0x5a, 0x5a, 0x5a, 255), width=max(1, int(1.2 * s)))
    d.rounded_rectangle([int(52 * s), int(129 * s), int(114 * s), int(169 * s)],
                         radius=int(2 * s), fill=(0x0a, 0x0a, 0x14, 255))
    for x in [58, 66, 74, 82]:
        d.rectangle([int(x * s), int(137 * s), int((x + 3) * s), int(161 * s)],
                    fill=(0xd4, 0xaf, 0x37, 255))
    d.rectangle([int(44 * s), int(120 * s), int(122 * s), int(123 * s)],
                fill=(0xff, 0xff, 0xff, 90))

    wave_color = (0x4e, 0xc9, 0xb0, 255)
    pts = [(122, 149), (138, 149), (138, 128), (158, 128),
           (158, 170), (178, 170), (178, 128), (198, 128),
           (198, 149), (218, 149)]
    pts = [(int(x * s), int(y * s)) for x, y in pts]
    d.line(pts, fill=wave_color, width=max(2, int(3.5 * s)), joint='curve')

    w = max(2, int(2.5 * s))
    tx = (0x4e, 0xc9, 0xb0, 255)
    rx = (0x56, 0x9c, 0xd6, 255)
    d.line([(int(138 * s), int(206 * s)), (int(218 * s), int(206 * s))], fill=tx, width=w)
    d.line([(int(210 * s), int(200 * s)), (int(218 * s), int(206 * s))], fill=tx, width=w)
    d.line([(int(210 * s), int(212 * s)), (int(218 * s), int(206 * s))], fill=tx, width=w)
    d.line([(int(122 * s), int(224 * s)), (int(42 * s), int(224 * s))], fill=rx, width=w)
    d.line([(int(50 * s), int(218 * s)), (int(42 * s), int(224 * s))], fill=rx, width=w)
    d.line([(int(50 * s), int(230 * s)), (int(42 * s), int(224 * s))], fill=rx, width=w)
    return img


def _draw_view(size: int) -> Image.Image:
    img = _bg_gradient(size)
    d = ImageDraw.Draw(img)
    s = size / 256.0

    d.rounded_rectangle([int(48 * s), int(44 * s), int(208 * s), int(49 * s)],
                         radius=int(2.5 * s), fill=(0, 122, 204, 255))

    # 搜索框
    d.rounded_rectangle([int(48 * s), int(60 * s), int(208 * s), int(80 * s)],
                         radius=int(4 * s), fill=(0x2d, 0x2d, 0x2d, 255),
                         outline=(0x3c, 0x3c, 0x3c, 255), width=max(1, int(1 * s)))
    if size >= 64:
        try:
            font_s = ImageFont.truetype(
                '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
                int(9 * s))
            d.text((int(72 * s), int(64 * s)), 'filter logs...',
                   fill=(0x66, 0x66, 0x66, 255), font=font_s)
        except Exception:
            pass
    d.ellipse([int(55 * s), int(65.5 * s), int(65 * s), int(75.5 * s)],
              outline=(0x88, 0x88, 0x88, 255), width=max(1, int(1.4 * s)))

    # 后层文档
    back_pts = [(132, 96), (196, 96), (214, 114), (214, 220), (132, 220)]
    back_pts = [(int(x * s), int(y * s)) for x, y in back_pts]
    d.polygon(back_pts, fill=(0x0f, 0x1a, 0x14, 255),
              outline=(0x3a, 0x3a, 0x3a, 255))

    # 前层文档
    front_pts = [(46, 110), (168, 110), (186, 128), (186, 224), (46, 224)]
    front_pts = [(int(x * s), int(y * s)) for x, y in front_pts]
    d.polygon(front_pts, fill=(0x1e, 0x1e, 0x1e, 255),
              outline=(0x4e, 0xc9, 0xb0, 255), width=max(1, int(2 * s)))
    d.line([(int(168 * s), int(110 * s)), (int(168 * s), int(128 * s))],
           fill=(0x4e, 0xc9, 0xb0, 255), width=max(1, int(2 * s)))
    d.line([(int(168 * s), int(128 * s)), (int(186 * s), int(128 * s))],
           fill=(0x4e, 0xc9, 0xb0, 255), width=max(1, int(2 * s)))

    # 行号侧栏
    d.rectangle([int(46 * s), int(110 * s), int(60 * s), int(224 * s)],
               fill=(0, 0, 0, 130))

    def bar(x, y, w, c, h=3):
        d.rectangle([int(x * s), int(y * s), int((x + w) * s), int((y + h) * s)],
                    fill=c + (255,))
    ts = (0x6a, 0x99, 0x55); info = (0x56, 0x9c, 0xd6)
    warn = (0xce, 0x91, 0x78); err = (0xf4, 0x47, 0x47)
    ok = (0x4e, 0xc9, 0xb0); hi = (0xdc, 0xdc, 0xaa)
    bar(64, 135, 40, ts); bar(108, 135, 60, info)
    d.rectangle([int(64 * s), int(146 * s), int(168 * s), int(157 * s)],
                fill=(0x61, 0x4d, 0x1e, 200))
    bar(64, 151, 50, hi); bar(118, 151, 48, hi)
    bar(64, 166, 35, warn); bar(103, 166, 55, info)
    bar(64, 184, 30, err); bar(98, 184, 65, info)
    bar(64, 202, 48, ok); bar(116, 202, 50, info)

    # 后层文档线条
    bar(142, 120, 60, ok); bar(142, 135, 50, info)
    bar(142, 150, 65, ok); bar(142, 165, 45, hi)

    # 大放大镜
    glow_r = int(20 * s)
    cx, cy = int(206 * s), int(206 * s)
    d.ellipse([cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r],
              fill=(0x1e, 0x1e, 0x1e, 255), outline=(0xdc, 0xdc, 0xaa, 255),
              width=max(2, int(3.5 * s)))
    d.line([(int(220 * s), int(220 * s)), (int(234 * s), int(234 * s))],
           fill=(0xdc, 0xdc, 0xaa, 255), width=max(3, int(5 * s)))
    return img


def _render(svg_path: str, fallback_draw, base_name: str):
    print(f'==> 渲染 {base_name}')
    pngs = []
    for sz in SIZES:
        img = _try_cairosvg_render(svg_path, sz)
        if img is None:
            img = fallback_draw(sz)
        pngs.append((sz, img))
        out = os.path.join(ASSETS, f'{base_name}-{sz}.png')
        img.save(out, 'PNG')
        print(f'  {out}: {os.path.getsize(out)} bytes')
    main = next(img for sz, img in pngs if sz == 256)
    main_path = os.path.join(ASSETS, f'{base_name}.png')
    main.save(main_path, 'PNG')
    print(f'  {main_path}: {os.path.getsize(main_path)} bytes')
    ico_path = os.path.join(ASSETS, f'{base_name}.ico')
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    main.save(ico_path, format='ICO', sizes=ico_sizes)
    print(f'  {ico_path}: {os.path.getsize(ico_path)} bytes')
    try:
        icns_path = os.path.join(ASSETS, f'{base_name}.icns')
        big = next(img for sz, img in pngs if sz == 1024).resize((1024, 1024), Image.LANCZOS)
        big.save(icns_path, format='ICNS')
        print(f'  {icns_path}: {os.path.getsize(icns_path)} bytes')
    except Exception as e:
        print(f'  ICNS 生成失败: {e}')


def main():
    debug_svg = os.path.join(ASSETS, 'icon_debug.svg')
    view_svg = os.path.join(ASSETS, 'icon_view.svg')
    _render(debug_svg, _draw_debug, 'icon_debug')
    _render(view_svg, _draw_view, 'icon_view')

    # 兼容旧名 icon.png / icon.ico / icon.icns / icon.svg / icon-48.png -> 用 debug
    debug_256 = Image.open(os.path.join(ASSETS, 'icon_debug.png'))
    debug_256.save(os.path.join(ASSETS, 'icon.png'))
    debug_48 = Image.open(os.path.join(ASSETS, 'icon_debug-48.png'))
    debug_48.save(os.path.join(ASSETS, 'icon-48.png'))
    debug_256.save(os.path.join(ASSETS, 'icon.ico'), format='ICO',
                   sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                          (64, 64), (128, 128), (256, 256)])
    debug_256.resize((1024, 1024), Image.LANCZOS).save(
        os.path.join(ASSETS, 'icon.icns'), format='ICNS')
    shutil.copy(debug_svg, os.path.join(ASSETS, 'icon.svg'))
    print('==> 完成')


if __name__ == '__main__':
    main()
