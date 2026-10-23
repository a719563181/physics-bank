; ============================================================
; PhysicsBank.iss — Inno Setup 安装脚本
; 高中物理题库管理（离线版）安装包
;
; 编译：ISCC.exe PhysicsBank.iss
; 产物：build\installer\高中物理题库管理_安装包_v1.0.6.exe
;
; 前置：已用 PyInstaller 打出 build\dist\PhysicsBank\
; ============================================================

#define MyAppName "高中物理题库管理"
#define MyAppShortName "物理题库"
; ★ 绝对根目录：ISCC 会把相对路径按 .iss 所在目录解析，
;   而 .iss 在 build\ 下，相对 "dist\..." 会命中 build\dist\（旧产物！）
;   所以这里显式用绝对路径，杜绝打包到过期代码。
;   2026-10-22：项目从 E: 迁到 F:，硬编码又崩了一次 ——
;   改成从 .iss 自身位置反推（.iss 在 <root>\build\ 下），后续再换盘不用改。
#define ProjRoot SourcePath + "\.."

#define MyAppVersion "1.2.1"
#define MyAppPublisher "个人自用"
#define MyAppExeName "PhysicsBank.exe"

[Setup]
AppId={{8F3A2C71-5D64-4B9E-A1F2-7C0E9B4D3A58}
AppName={#MyAppName}（离线版）
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 允许用户选择安装目录
AllowNoIcons=yes
; 输出
OutputDir=installer
OutputBaseFilename={#MyAppName}_安装包_v{#MyAppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 64 位应用
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 需要管理员权限才可写入 Program Files
PrivilegesRequired=admin
; 安装最小体积
ExtraDiskSpaceRequired=0
DisableWelcomePage=no
LicenseFile=

[Languages]
Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："; Flags: checkedonce
Name: "quicklaunchicon"; Description: "创建快速启动栏图标"; GroupDescription: "附加图标："; Flags: unchecked

[Files]
; ---------- 主程序（PyInstaller onedir 产物）----------
Source: "{#ProjRoot}\dist\PhysicsBank\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; ---------- 便携说明 ----------
Source: "{#ProjRoot}\build\README_安装说明.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: quicklaunchicon

[Run]
; 安装完成后可选立即启动
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时清掉程序目录残留（用户数据在 %LOCALAPPDATA%，默认保留）
Type: filesandordirs; Name: "{app}\_internal"

[Messages]
chinese.WelcomeLabel1=欢迎安装 [name]
chinese.WelcomeLabel2=即将在你的电脑上安装 [name]。[name] 是一款离线运行的高中物理题库管理软件，支持题目录入、公式渲染、配图、组卷，并导出 Word / PDF / PPT 试卷。%n%n建议关闭其他程序后再继续。
chinese.FinishedLabel=安装完成，[name] 已经可以使用了。
