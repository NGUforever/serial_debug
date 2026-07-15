"""从 icon.png 生成 icon.ico（Windows）和 icon.icns（macOS）。
用法：python3 _gen_icons.py
"""
import os
from PIL import Image

src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   'assets', 'icon.png')
img = Image.open(src).convert('RGBA')
print(f'源图标: {img.size}, 模式: {img.mode}')

# ---- Windows .ico ----
ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'assets', 'icon.ico')
sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
         (128, 128), (256, 256)]
img.save(ico_path, format='ICO', sizes=sizes)
print(f'生成 {ico_path}: {os.path.getsize(ico_path)} bytes')

# ---- macOS .icns ----
icns_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'assets', 'icon.icns')
# Pillow 支持 .icns 格式，但需要特定尺寸
icns_sizes = [(16, 16), (32, 32), (64, 64), (128, 128), (256, 256),
              (512, 512), (1024, 1024)]
# 先放大到 1024 再保存
big = img.resize((1024, 1024), Image.LANCZOS)
try:
    big.save(icns_path, format='ICNS')
    print(f'生成 {icns_path}: {os.path.getsize(icns_path)} bytes')
except Exception as e:
    print(f'.icns 生成失败（macOS 构建机上用 iconutil 生成）: {e}')

# 验证 .ico
ico = Image.open(ico_path)
print(f'验证 .ico: format={ico.format}')
