; NumpadToMIDI - Windows all-in-one installer (Inno Setup 6.3+)
;
; Build (from the repository root, after `pyinstaller build.spec`):
;   ISCC.exe /DAppVersion=1.4.1 installer\windows_installer.iss
; Output: dist\NumpadToMIDI-Setup-v<AppVersion>.exe
;
; The installer
;   * installs NumpadToMIDI (admin elevation, Start Menu + Desktop shortcuts)
;   * downloads the official loopMIDI package (which installs the teVirtualMIDI
;     driver) from tobias-erichsen.de at install time, verifies its SHA-256 and
;     installs it silently - ONLY if the driver is not already present.
;     The vendor's binaries are never stored in this repository or bundled in
;     the installer (see installer\driver_notice.txt).

#ifndef AppVersion
  #define AppVersion "1.5.5"
#endif
#define AppName      "NumpadToMIDI"
#define AppExeName   "NumpadToMIDI.exe"
#define AppUrl       "https://github.com/homejes/NumpadToMIDI"

; Official vendor package. SHA-256 is pinned: if the vendor replaces the file the
; download is rejected and the installer continues without the driver.
#define DriverUrl    "https://www.tobias-erichsen.de/wp-content/uploads/2020/01/loopMIDISetup_1_0_16_27.zip"
#define DriverSha256 "975399eb76e2c5a1d553ecd8975ab361261ccab8fd9412b8060b19877cb4e0d5"
#define DriverDll    "teVirtualMIDI64.dll"

[Setup]
AppId={{6F1D2B8E-3C54-4B1A-9E27-5A8C0D4F7E31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=NumpadToMIDI
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\icon.ico
UninstallDisplayName={#AppName}
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
InfoBeforeFile=driver_notice.txt
OutputDir=..\dist
OutputBaseFilename=NumpadToMIDI-Setup-v{#AppVersion}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "installdriver"; Description: "Install the virtual MIDI driver (teVirtualMIDI by Tobias Erichsen, downloaded from the vendor) if it is missing"; GroupDescription: "Virtual MIDI:"

[Files]
Source: "..\dist\NumpadToMIDI-Windows.exe"; DestDir: "{app}"; DestName: "{#AppExeName}"; Flags: ignoreversion
Source: "..\assets\icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "driver_notice.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
var
  DriverZip: String;

function DriverPresent: Boolean;
var
  Count: Cardinal;
begin
  // {sys} is the native 64-bit System32 because of ArchitecturesInstallIn64BitMode.
  // Also check that the teVirtualMIDI64 PnP device node is enumerated (Count > 0).
  Result := FileExists(ExpandConstant('{sys}\{#DriverDll}')) and
            RegQueryDWordValue(HKEY_LOCAL_MACHINE, 'SYSTEM\CurrentControlSet\Services\teVirtualMIDI64\Enum', 'Count', Count) and
            (Count > 0);
end;

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  if (ProgressMax > 0) and (Progress = ProgressMax) then
    Log(Format('Downloaded %s', [FileName]));
  Result := True;
end;

procedure RestartDriverDevice;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\pnputil.exe'), '/restart-device "ROOT\MEDIA\0000"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Log(Format('pnputil /restart-device returned code: %d', [ResultCode]));
end;

// Runs for both interactive and silent installs, right before files are copied.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  DriverZip := '';
  // Ensure any running NumpadToMIDI process (including tray) releases driver handles
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM NumpadToMIDI.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if not WizardIsTaskSelected('installdriver') then
  begin
    Log('Driver task not selected - skipping virtual MIDI driver.');
    Exit;
  end;
  if DriverPresent then
  begin
    Log('teVirtualMIDI driver already present - skipping download.');
    RestartDriverDevice;
    Exit;
  end;
  try
    WizardForm.PreparingLabel.Caption := 'Downloading the virtual MIDI driver...';
    WizardForm.PreparingLabel.Visible := True;
    DownloadTemporaryFile('{#DriverUrl}', 'loopMIDISetup.zip', '{#DriverSha256}', @OnDownloadProgress);
    DriverZip := ExpandConstant('{tmp}\loopMIDISetup.zip');
  except
    Log('Driver download failed: ' + GetExceptionMessage);
    SuppressibleMsgBox(
      'The virtual MIDI driver could not be downloaded:' + #13#10 + GetExceptionMessage + #13#10#13#10 +
      'NumpadToMIDI will still be installed. To get a virtual MIDI port later, install loopMIDI ' +
      'from tobias-erichsen.de or run this installer again with an internet connection.',
      mbError, MB_OK, IDOK);
  end;
end;

procedure InstallDriver;
var
  ExtractDir, SetupExe, PsArgs: String;
  ResultCode: Integer;
begin
  if DriverZip = '' then Exit;
  ExtractDir := ExpandConstant('{tmp}\loopmidi');
  PsArgs := '-NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath ''' +
            DriverZip + ''' -DestinationPath ''' + ExtractDir + ''' -Force"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), PsArgs, '',
              SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
  begin
    Log(Format('Extracting driver package failed (code %d).', [ResultCode]));
    Exit;
  end;
  SetupExe := ExtractDir + '\loopMIDISetup.exe';
  if not FileExists(SetupExe) then
  begin
    Log('loopMIDISetup.exe not found in downloaded package.');
    Exit;
  end;
  WizardForm.StatusLabel.Caption := 'Installing the virtual MIDI driver...';
  // the vendor package is a WiX bundle: /quiet /norestart is its silent mode
  if Exec(SetupExe, '/install /quiet /norestart', ExtractDir, SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Log(Format('Driver installer exit code: %d', [ResultCode]));
    if (not DriverPresent) and ((ResultCode = 0) or (ResultCode = 1638)) then
    begin
      Log('Driver PnP node still missing after /install; attempting /repair...');
      Exec(SetupExe, '/repair /quiet /norestart', ExtractDir, SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Log(Format('Driver repair exit code: %d', [ResultCode]));
    end;
    case ResultCode of
      0, 1638: ; // installed / newer version already present
      3010: SuppressibleMsgBox('Windows must be restarted to finish installing the virtual MIDI driver.',
                               mbInformation, MB_OK, IDOK);
    else
      SuppressibleMsgBox(Format('The virtual MIDI driver installer returned code %d. NumpadToMIDI is installed; ' +
                         'you can install loopMIDI manually if no virtual port appears.', [ResultCode]),
                         mbError, MB_OK, IDOK);
    end;
  end
  else
    Log('Could not start the driver installer: ' + SysErrorMessage(ResultCode));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    InstallDriver;
    RestartDriverDevice;
  end;
end;
