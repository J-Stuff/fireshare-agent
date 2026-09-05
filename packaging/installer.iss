; Fireshare Agent installer.
;
; Build with (from the repo root, after the PyInstaller onedir build in dist\FireshareAgent
; already exists - see fireshare_agent.spec):
;   iscc /DMyAppVersion=1.2.3 packaging\installer.iss
;
; MyAppVersion falls back to 0.0.0-dev when not passed in, for local test compiles.
;
; Supports both install modes from a single installer, matching how the app is used - a personal
; single-user tray tool that shouldn't require admin, but should still be installable per-machine
; for anyone who wants that:
;   - Per-user (default, no admin required): installs under {localappdata}\Programs.
;   - Per-machine (admin required): installs under Program Files.
; PrivilegesRequiredOverridesAllowed lets the interactive installer show a page for the user to
; choose, and lets a silent invocation choose via the /CURRENTUSER or /ALLUSERS command-line
; switch (used by the app's own self-updater to repeat whichever mode it was originally installed
; with, so an unattended update doesn't need to ask again). Requires Inno Setup 6.1+.

#define MyAppName "Fireshare Agent"
#define MyAppExeName "FireshareAgent.exe"
#define MyAppPublisher "J-Stuff"
#define MyAppURL "https://github.com/J-Stuff/fireshare-agent"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif
#define RepoRoot SourcePath + "..\"

[Setup]
AppId={{6C1A4C9E-6E3B-4C9C-9C7B-8E1D9B0A2F3D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir={#RepoRoot}dist\installer
OutputBaseFilename=FireshareAgent-Setup-{#MyAppVersion}
SetupIconFile={#RepoRoot}img\icon.ico
LicenseFile={#RepoRoot}LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline dialog
; Uses Windows Restart Manager to detect/close a running FireshareAgent.exe that would otherwise
; block overwriting its files - a safety net for the interactive case (the self-updater already
; closes the app itself before launching this installer).
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#RepoRoot}dist\FireshareAgent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; No postinstall/skipifsilent flags: this must also fire on a fully silent run (/VERYSILENT),
; since that's how the self-updater relaunches the app after installing a new version in the
; background. runasoriginaluser matters when Setup itself is running elevated (the per-machine
; mode) - without it the app would launch running as admin instead of as the normal signed-in
; user, which a background tray app should never do.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait runasoriginaluser
