@echo off
REM Windows 安装脚本：将 serial_debug 安装到 %LOCALAPPDATA% 并创建开始菜单快捷方式
REM
REM 用法：
REM   install_win.bat                  安装
REM   install_win.bat --uninstall      卸载
REM
REM 安装后：
REM   - 程序目录：%LOCALAPPDATA%\serial_debug\serial_debug.exe
REM   - 开始菜单：出现"串口调试助手"快捷方式（可右键固定到任务栏）
REM   - 命令行：serial_debug（自动加入 PATH，新开 cmd 即可用）
REM
REM 需要先运行 build_win.bat 生成 dist\serial_debug\ 目录

setlocal enabledelayedexpansion

cd /d %~dp0..
set "PROJECT_DIR=%CD%"

set "ACTION=install"
if /i "%~1"=="--uninstall" set "ACTION=uninstall"
if /i "%~1"=="-u" set "ACTION=uninstall"

set "INSTALL_DIR=%LOCALAPPDATA%\serial_debug"
set "BIN_DIR=%LOCALAPPDATA%\bin"
set "STARTMENU_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs"

if "%ACTION%"=="uninstall" (
    echo ==^> 卸载 serial_debug...
    rmdir /s /q "%INSTALL_DIR%" 2>nul
    del /q "%BIN_DIR%\serial_debug.bat" 2>nul
    del /q "%STARTMENU_DIR%\串口调试助手.lnk" 2>nul
    echo ==^> 卸载完成
    pause
    exit /b 0
)

REM 检查构建产物
if not exist "%PROJECT_DIR%\dist\serial_debug\serial_debug.exe" (
    echo 错误：dist\serial_debug\serial_debug.exe 不存在，请先运行 packaging\build_win.bat
    pause
    exit /b 1
)

echo ==^> 安装 serial_debug 到 %INSTALL_DIR% ...
mkdir "%INSTALL_DIR%" 2>nul
xcopy /e /y /i "%PROJECT_DIR%\dist\serial_debug\*" "%INSTALL_DIR%\" >nul

REM 1. 创建命令行启动批处理
echo ==^> 注册命令 serial_debug 到 PATH...
mkdir "%BIN_DIR%" 2>nul
set "WRAPPER=%BIN_DIR%\serial_debug.bat"
> "%WRAPPER%" echo @echo off
>> "%WRAPPER%" echo "%INSTALL_DIR%\serial_debug.exe" %%*
>> "%WRAPPER%" echo exit /b

REM 将 %LOCALAPPDATA%\bin 加入用户 PATH（PowerShell）
echo ==^> 更新用户 PATH 环境变量...
powershell -NoProfile -Command ^
    "$p = [Environment]::GetEnvironmentVariable('Path', 'User'); " ^
    "if ($p -notlike '*%BIN_DIR%*') { " ^
    "    [Environment]::SetEnvironmentVariable('Path', $p + ';%BIN_DIR%', 'User'); " ^
    "    Write-Host 'PATH 已更新（需重新打开 cmd 生效）'; " ^
    "} else { " ^
    "    Write-Host 'PATH 已包含路径'; " ^
    "}"

REM 2. 创建开始菜单快捷方式（带图标）
echo ==^> 创建开始菜单快捷方式...
powershell -NoProfile -Command ^
    "$ws = New-Object -ComObject WScript.Shell; " ^
    "$lnk = $ws.CreateShortcut('%STARTMENU_DIR%\串口调试助手.lnk'); " ^
    "$lnk.TargetPath = '%INSTALL_DIR%\serial_debug.exe'; " ^
    "$lnk.WorkingDirectory = '%INSTALL_DIR%'; " ^
    "$lnk.IconLocation = '%INSTALL_DIR%\_internal\serial_debug\resources\assets\icon.ico,0'; " ^
    "$lnk.Description = '串口调试工具：CH343 实时收发 / AT 命令管理 / 日志存储'; " ^
    "$lnk.Save(); " ^
    "Write-Host '快捷方式已创建: %STARTMENU_DIR%\串口调试助手.lnk'"

REM 3. 可选：创建桌面快捷方式
if /i "%~2"=="--desktop" (
    echo ==^> 创建桌面快捷方式...
    powershell -NoProfile -Command ^
        "$ws = New-Object -ComObject WScript.Shell; " ^
        "$desktop = [Environment]::GetFolderPath('Desktop'); " ^
        "$lnk = $ws.CreateShortcut(\"$desktop\串口调试助手.lnk\"); " ^
        "$lnk.TargetPath = '%INSTALL_DIR%\serial_debug.exe'; " ^
        "$lnk.WorkingDirectory = '%INSTALL_DIR%'; " ^
        "$lnk.IconLocation = '%INSTALL_DIR%\_internal\serial_debug\resources\assets\icon.ico,0'; " ^
        "$lnk.Save()"
)

echo.
echo ==^> 安装完成！
echo.
echo 使用方式：
echo   1. 命令行：serial_debug（重新打开 cmd 后生效）
echo   2. 开始菜单：搜索"串口调试助手"，可右键固定到任务栏
echo   3. 直接运行：%INSTALL_DIR%\serial_debug.exe
echo.
echo 图标位置：%INSTALL_DIR%\_internal\serial_debug\resources\assets\icon.ico
echo.
pause
