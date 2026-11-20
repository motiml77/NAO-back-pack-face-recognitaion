; Remote Printer - Windows installer (Inno Setup 6) for Windows 10 (1809+) and Windows 11, 64-bit.
;
; Fully automatic: no pages and no questions (Windows asks "Yes" once - UAC - because the program
; is installed for the whole computer). Everything is inside: the program, the Python runtime and
; libraries, and SumatraPDF. The program then runs as a scheduled task under the SYSTEM account:
; it starts at every boot - before anybody logs in - with full permissions, and runs right away
; after the installation (no restart).

#define AppName "הדפסה מרחוק"
#define AppExe "RemotePrinter.exe"
#define SiteUrl "https://motiml77.github.io/NAO-back-pack-face-recognitaion/"
#ifndef AppVersion
  #define AppVersion "3.5"
#endif

[Setup]
AppId={{6E0B7C1A-5D2F-4E8B-9A33-2B6A4F1C9D70}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Remote Printer
AppPublisherURL={#SiteUrl}
DefaultDirName={autopf}\RemotePrinter
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
ShowLanguageDialog=no
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
CloseApplications=no
RestartApplications=no
OutputDir=..\dist
OutputBaseFilename=RemotePrinter-Setup
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
SetupLogging=yes

[Languages]
Name: "he"; MessagesFile: "compiler:Languages\Hebrew.isl"

[Files]
Source: "..\dist\RemotePrinter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "SumatraPDF.exe"; DestDir: "{app}"; Flags: ignoreversion

[Run]
; registers the startup task (SYSTEM, at boot), power settings, and starts the program now
Filename: "{app}\{#AppExe}"; Parameters: "--setup"; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveTasks"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopRemotePrinter"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{commonappdata}\RemotePrinter"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  // stop a running copy so its files can be replaced (settings in ProgramData are kept)
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/End /TN RemotePrinter', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1000);
  Result := '';
end;
