; Inno Setup 6 script for NetBox Synchronizer.
; Built by build.ps1, which passes the version: ISCC /DAppVersion=4.1.1 NetBoxSynchronizer.iss

#define AppName "NetBox Synchronizer"
#ifndef AppVersion
  #define AppVersion "4.16.4"
#endif
#define AppPublisher "Louis-Alexander Kerst"
#define AppExeName "NetBoxSynchronizer.exe"
#define AppUserModelID "AlexanderKerst.NetBoxSynchronizer"
#define SourceDir "..\dist\NetBoxSynchronizer"
#define ConfigDir "NetBox VM Import"

[Setup]
AppId={{6F0B8E3C-4A9D-4C1E-9B7A-2D5E8F1A3C47}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Installs per user without admin rights; the wizard offers "all users" as well.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=NetBoxSynchronizer-Setup-{#AppVersion}
SetupIconFile=..\netbox_sync\resources\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=110
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoCompany={#AppPublisher}
ShowLanguageDialog=auto

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
german.DeleteUserData=Sollen auch die Kundenprofile (inklusive verschlüsselter Zugangsdaten) und Protokolle gelöscht werden?%n%nBei „Nein“ bleiben sie für eine spätere Neuinstallation erhalten.
english.DeleteUserData=Do you also want to delete the customer profiles (including encrypted credentials) and logs?%n%nChoose "No" to keep them for a later reinstallation.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Remove libraries of the previous version before copying the new build.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; AppUserModelID: "{#AppUserModelID}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; AppUserModelID: "{#AppUserModelID}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; Restart after an update started from within the application (silent mode).
Filename: "{app}\{#AppExeName}"; Flags: nowait; Check: WizardSilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
  begin
    DataDir := ExpandConstant('{userappdata}\{#ConfigDir}');
    if DirExists(DataDir) then
      if MsgBox(CustomMessage('DeleteUserData'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
