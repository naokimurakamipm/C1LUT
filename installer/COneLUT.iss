; C-One LUT installer script (Inno Setup).
; Build with build_installer.bat (finds ISCC.exe) or:
;   ISCC.exe installer\COneLUT.iss
; The setup exe lands in installer\Output\ and is NOT committed (gitignored).

#define MyAppName "C-One LUT"
#define MyAppVersion GetVersionNumbersString('..\dist\COneLUT\COneLUT.exe')
#define MyAppPublisher "Naoki Murakami"
#define MyAppExeName "COneLUT.exe"

[Setup]
AppId={{9F5C8C2B-3A47-4E86-9D1B-6C0E5A7B2F10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={userpf}\C-One LUT
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install: no UAC prompt, everything under the user's profile.
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=COneLUT-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\COneLUT\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\native\LCMS-LICENSE"; DestDir: "{app}"; DestName: "LCMS-LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
