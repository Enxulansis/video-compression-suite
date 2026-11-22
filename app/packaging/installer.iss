#ifndef BundleDir
  #error BundleDir is required
#endif
#ifndef DependencyDir
  #error DependencyDir is required
#endif
#ifndef ReleaseDir
  #error ReleaseDir is required
#endif

[Setup]
AppId={{94F95108-4886-4E73-B7D7-4398700F8CD7}
AppName=视频压缩套件
AppVersion=0.7.1
AppVerName=视频压缩套件 0.7.1
VersionInfoVersion=0.7.1.0
DefaultDirName={localappdata}\Programs\VideoCompressionSuite
DefaultGroupName=视频压缩套件
PrivilegesRequired=lowest
MinVersion=10.0.19041
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
OutputDir={#ReleaseDir}
OutputBaseFilename=视频压缩套件_0.7.1_x64_离线完整版
Compression=lzma2/fast
LZMADictionarySize=8192
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\VideoCompressionSuite.exe
SetupLogging=yes
CloseApplications=no
RestartApplications=no
AppMutex=VideoCompressionSuite.Desktop
SetupMutex=VideoCompressionSuite.Setup
DisableProgramGroupPage=yes
DisableWelcomePage=no
UsePreviousLanguage=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#DependencyDir}\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; Flags: dontcopy nocompression

[Icons]
Name: "{autoprograms}\视频压缩套件"; Filename: "{app}\VideoCompressionSuite.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\视频压缩套件"; Filename: "{app}\VideoCompressionSuite.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{autoprograms}\视频压缩套件使用说明"; Filename: "{app}\_internal\使用说明.txt"

[Run]
Filename: "{app}\VideoCompressionSuite.exe"; Description: "启动视频压缩套件"; Flags: nowait postinstall skipifsilent unchecked runasoriginaluser

[Code]
const
  WebViewKey = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function CreateFile(Name: String; Access, Share: Cardinal; Security: NativeInt;
                    Creation, Flags: Cardinal; Template: NativeInt): NativeInt;
  external 'CreateFileW@kernel32.dll stdcall';
function CloseHandle(Handle: NativeInt): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function CanReplaceFile(Name: String): Boolean;
var
  Handle: NativeInt;
begin
  Result := True;
  if not FileExists(Name) then Exit;
  { Request write access without writing any bytes. Windows denies this for a
    running executable's image section, including orphan ffmpeg processes. }
  Handle := CreateFile(Name, $40000000, 7, 0, 3, $80, 0);
  Result := Handle <> -1;
  if Result then CloseHandle(Handle);
end;

function CanReplaceApplication: Boolean;
begin
  Result := CanReplaceFile(ExpandConstant('{app}\VideoCompressionSuite.exe'))
        and CanReplaceFile(ExpandConstant('{app}\_internal\bin\ffmpeg.exe'))
        and CanReplaceFile(ExpandConstant('{app}\_internal\bin\ffprobe.exe'));
end;

function InitializeUninstall: Boolean;
begin
  Result := not CheckForMutexes('VideoCompressionSuite.Setup');
  if Result then begin
    CreateMutex('VideoCompressionSuite.Setup');
    Result := CanReplaceApplication;
  end;
  if not Result then
    SuppressibleMsgBox('程序或转码工具仍在运行，或文件暂时不可写。请等待任务结束、关闭程序后再卸载。', mbError, MB_OK, IDOK);
end;

function HasWebViewAt(RootKey: Integer): Boolean;
var
  Version: String;
  DotPosition, Major: Integer;
begin
  Result := False;
  if RegQueryStringValue(RootKey, WebViewKey, 'pv', Version) then begin
    DotPosition := Pos('.', Version);
    if DotPosition > 1 then begin
      Major := StrToIntDef(Copy(Version, 1, DotPosition - 1), 0);
      Result := Major >= 120;
    end;
  end;
end;

function HasWebView: Boolean;
begin
  Result := HasWebViewAt(HKLM32) or HasWebViewAt(HKCU32) or HasWebViewAt(HKCU64);
end;

function InitializeSetup: Boolean;
var
  Release: Cardinal;
begin
  Result := RegQueryDWordValue(HKLM32, 'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full', 'Release', Release)
            and (Release >= 528040);
  if not Result then
    SuppressibleMsgBox('当前 Windows 的 .NET Framework 4.8 系统组件缺失或已损坏。请先完成 Windows 系统更新后重新安装。', mbError, MB_OK, IDOK);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Result := '';
  if not CanReplaceApplication then begin
    Result := '程序或转码工具仍在运行，或文件暂时不可写。请等待任务结束、关闭程序后再安装；不会停止现有任务。';
    Exit;
  end;
  if not HasWebView then begin
    WizardForm.StatusLabel.Caption := '正在安装内置的 WebView2 运行环境，请稍候…';
    ExtractTemporaryFile('MicrosoftEdgeWebView2RuntimeInstallerX64.exe');
    if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebView2RuntimeInstallerX64.exe'),
                '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, Code) then begin
      Result := '无法启动内置 WebView2 安装程序。错误代码：' + IntToStr(Code);
      Exit;
    end;
    if not HasWebView then begin
      Result := 'WebView2 运行环境安装未完成，程序尚不能使用。错误代码：' + IntToStr(Code)
                + '。请检查 Windows 安装策略或重新运行安装包。';
      Exit;
    end;
    NeedsRestart := Code = 3010;
  end;
end;
