; ══════════════════════════════════════════════════════════════
;  大肥鱼 安装程序
;  需要 Inno Setup 6+（https://jrsoftware.org/isdl.php）
;
;  编译前先跑 PyInstaller，产物在 dist\大肥鱼\
;  然后：iscc packaging\installer.iss
; ══════════════════════════════════════════════════════════════

#define AppName "大肥鱼"
#define AppNameEn "BigFish"
#define AppVersion "0.1.0"
#define AppPublisher "Alime"
#define AppExe "大肥鱼.exe"
#define AppId "{{A7F3C2D1-8B45-4E6A-9C02-1D5E8F3B7A94}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppNameEn}
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=..\dist
OutputBaseFilename=BigFish-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
SetupIconFile=..\ui\icon.ico
UninstallDisplayName={#AppName}

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: checkedonce

[Files]
; 主程序与依赖
Source: "..\dist\大肥鱼\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; 首次安装后不问配置，直接启动；配置在界面里做
Filename: "{app}\{#AppExe}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 清理运行产物，但保留用户配置
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\.tmp"
Type: files; Name: "{app}\*.log"

[Code]
// ─────────────────────────────────────────────────────────────
// 安装前检查：微信是否在运行
// 微信在跑时替换 DLL 相关文件会失败，提示用户先退出
// ─────────────────────────────────────────────────────────────
function IsWeixinRunning(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec('cmd.exe', '/c tasklist /FI "IMAGENAME eq Weixin.exe" | find /I "Weixin.exe"',
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if IsWeixinRunning() then
  begin
    if MsgBox('检测到微信正在运行。' + #13#10 + #13#10 +
              '安装过程不会影响微信，但建议先退出微信，' + #13#10 +
              '装完再打开，避免个别文件被占用。' + #13#10 + #13#10 +
              '继续安装？', mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;
