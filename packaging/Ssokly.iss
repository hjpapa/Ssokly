#ifndef AppSource
  #define AppSource "..\dist\Ssokly"
#endif
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef ReleaseOutput
  #define ReleaseOutput "..\dist"
#endif

[Setup]
AppId={{755C3585-5F6A-48F5-BAEA-0A57BB25B1C9}
AppName=Ssokly
AppVersion={#AppVersion}
AppPublisher=docsusil
DefaultDirName={localappdata}\Programs\Ssokly
DefaultGroupName=Ssokly
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#ReleaseOutput}
OutputBaseFilename=Ssokly-Setup-{#AppVersion}-x64
SetupIconFile=..\assets\ssokly-capture-mark.ico
#ifdef SignedRelease
SignTool=ssokly
SignedUninstaller=yes
#endif
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Ssokly.exe
CloseApplications=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: desktopicon; Description: "바탕화면 바로가기 만들기"; Flags: unchecked

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Ssokly"; Filename: "{app}\Ssokly.exe"
Name: "{autodesktop}\Ssokly"; Filename: "{app}\Ssokly.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Ssokly.exe"; Description: "Ssokly 실행"; Flags: nowait postinstall skipifsilent
