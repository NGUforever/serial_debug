@echo off
REM Windows 构建脚本
REM
REM 用法：双击或在 cmd 执行 build_win.bat
REM
REM 依赖（自动安装）：
REM   pip install pyinstaller pyserial
REM
REM 输出：
REM   dist\serial_debug\              (onedir 目录)
REM   可用 Inno Setup 打包成安装器

setlocal
cd /d %~dp0..
set "PROJECT_DIR=%CD%"

REM 检测 VC++ Redistributable 2015-2022 x64（PySide6 必需）
REM 注册表 HKLM\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64\Installed=1
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v Installed /reg:64 >nul 2>&1
if errorlevel 1 (
    echo ==> 警告：VC^^+ Redistributable 2015-2022 x64 未装
    echo    PySide6 启动会崩 "缺 VCRuntime140.dll"
    echo    下载安装：https://aka.ms/vs/17/release/vc_redist.x64.exe
    echo    或继续构建，分发时让用户自行安装
    choice /c yn /n /m "继续构建？[y/N]: "
    if errorlevel 2 exit /b 1
)

echo ==> 安装依赖（pip 镜像源 fallback）...
python scripts\pip_install_with_fallback.py -r requirements.txt pyinstaller || goto :error

echo ==> PyInstaller 构建...
pyinstaller --clean --noconfirm packaging\serial_debug.spec || goto :error

echo.
echo ==> 完成！输出目录: dist\serial_debug\
echo.
echo 使用方式：
echo   1. 进入 dist\serial_debug\ 目录
echo   2. 双击 serial_debug.exe 运行
echo   3. 或将整个 serial_debug 目录压缩为 zip 分发
echo.
echo 如需打包成安装器：
echo   1. 安装 Inno Setup (https://jrsoftware.org/isdl.php)
echo   2. 用 Inno Setup Compiler 打开 packaging\serial_debug-installer.iss
echo   3. 编译生成 dist\serial_debug-setup.exe
echo   4. 输出安装包自带：开始菜单快捷方式 / PATH 注册 / .log 关联
goto :eof

:error
echo 构建失败
exit /b 1
