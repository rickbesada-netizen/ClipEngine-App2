; Install the packaged ClipEngine while preserving the existing application entry point.
[Setup]
AppId={{9A4F68E2-5D77-4232-BD9A-11C84A451E66}
AppName=ClipEngine
AppVersion={#GetEnv("CLIPENGINE_VERSION")}
AppPublisher=ClipEngine
DefaultDirName={localappdata}\Programs\ClipEngine
DefaultGroupName=ClipEngine
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=ClipEngineSetup
SetupIconFile=assets\ClipEngine.ico
UninstallDisplayIcon={app}\ClipEngine.exe
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: checkedonce

[Files]
Source: "dist\ClipEngine.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\ClipEngine"; Filename: "{app}\ClipEngine.exe"
Name: "{autodesktop}\ClipEngine"; Filename: "{app}\ClipEngine.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ClipEngine.exe"; Description: "Launch ClipEngine"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Do not delete clips or settings from %LOCALAPPDATA%\ClipEngine data.
