; Inno Setup 脚本 - 串口调试助手 Windows 安装包
;
; 用法：
;   1. 先运行 build_win.bat（或 pyinstaller --clean packaging/serial_debug.spec）生成 dist\serial_debug\ 目录
;   2. 安装 Inno Setup 6: https://jrsoftware.org/isdl.php
;   3. 用 Inno Setup Compiler 打开此 .iss 文件，编译生成安装 exe
;      命令行: iscc packaging\serial_debug-installer.iss
;   4. 输出：dist\serial_debug-windows-x64-setup.exe
;
; CI 用法（GitHub Actions windows-latest）：
;   choco install innosetup -y --no-progress
;   iscc packaging\serial_debug-installer.iss
;
; 功能：
;   - 安装到 %LOCALAPPDATA%\serial_debug（无需管理员权限）
;   - 开始菜单创建"串口调试助手"和"日志查看器"快捷方式
;   - 自动注册 PATH，命令行 serial_debug 可用
;   - 文件管理器右键 .log/.txt → 打开方式 → 日志查看器
;   - 控制面板可卸载
;
; 版本对齐：AppVersion 必须与 pyproject.toml [project].version 一致（当前 1.1.0）
; AppId（GUID）一经发布固定不变，升级安装时 Inno Setup 据此识别同一应用

[Setup]
AppName=串口调试助手
AppVersion=1.1.0
AppId={{B8F3A1D2-7E5C-4F9A-9B6E-3D2C1A0B5E7F}
AppPublisher=Ninebot
AppPublisherURL=https://www.ninebot.com
AppSupportURL=https://www.ninebot.com
AppUpdatesURL=https://www.ninebot.com
DefaultDirName={localappdata}\serial_debug
DefaultGroupName=串口调试助手
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=serial_debug-windows-x64-setup
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\serial_debug.exe
UninstallDisplayName=串口调试助手
VersionInfoVersion=1.1.0.0
VersionInfoProductVersion=1.1.0.0
LicenseFile=
InfoBeforeFile=

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项:"; Flags: checkedonce
Name: "associate"; Description: "关联 .log/.txt 文件到日志查看器"; GroupDescription: "附加选项:"; Flags: checkedonce

[Files]
; 整个 dist\serial_debug 目录（iss 在 packaging/ 下，相对路径回到仓库根）
Source: "..\dist\serial_debug\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 开始菜单：串口调试助手
Name: "{group}\串口调试助手"; Filename: "{app}\serial_debug.exe"; IconFilename: "{app}\_internal\serial_debug\resources\assets\icon_debug.ico"; Comment: "串口调试工具：CH343 实时收发 / AT 命令管理 / 日志存储"
; 开始菜单：日志查看器（通过 --view 参数）
Name: "{group}\日志查看器"; Filename: "{app}\serial_debug.exe"; Parameters: "--view"; IconFilename: "{app}\_internal\serial_debug\resources\assets\icon_view.ico"; Comment: "日志文件查看工具：多文件 Tab + 筛选面板 + 全局搜索"
; 桌面快捷方式（可选）
Name: "{commondesktop}\串口调试助手"; Filename: "{app}\serial_debug.exe"; IconFilename: "{app}\_internal\serial_debug\resources\assets\icon_debug.ico"; Tasks: desktopicon
Name: "{commondesktop}\日志查看器"; Filename: "{app}\serial_debug.exe"; Parameters: "--view"; IconFilename: "{app}\_internal\serial_debug\resources\assets\icon_view.ico"; Tasks: desktopicon

[Registry]
; 注册 PATH（用户级，无需管理员）
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{localappdata}\serial_debug"; Flags: dontcreatekey
; 文件关联（可选）
Root: HKCU; Subkey: "Software\Classes\.log"; ValueType: string; ValueName: ""; ValueData: "serial_debug_log"; Flags: uninsdeletevalue; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\.txt\OpenWithList\serial_debug"; ValueType: string; ValueName: ""; ValueData: ""; Flags: uninsdeletekey; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\serial_debug_log"; ValueType: string; ValueName: ""; ValueData: "日志文件"; Flags: uninsdeletekey; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\serial_debug_log\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\serial_debug.exe"" --view ""%1"""; Flags: uninsdeletekey; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\serial_debug_log\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\_internal\serial_debug\resources\assets\icon_view.ico"; Flags: uninsdeletekey; Tasks: associate

[Run]
; 安装后直接启动主程序（可选）
Filename: "{app}\serial_debug.exe"; Description: "立即启动串口调试助手"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 清理用户配置（可选，默认保留）
; Type: filesandordirs; Name: "{userappdata}\serial_debug"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
