; Remote Printer - Windows installer (Inno Setup 6)
;
; Fully automatic: no questions, no password, no administrator prompt.
; Everything is inside: the program, the Python runtime and libraries, SumatraPDF.
; After installing, the program runs immediately (no restart), starts hidden at every
; logon, and a watchdog task restarts it within 5 minutes if it ever stops.

#define AppName "הדפסה מרחוק"
#define AppExe "RemotePrinter.exe"
#define SiteUrl "https://motiml77.github.io/NAO-back-pack-face-recognitaion/"
#define Watchdog "RemotePrinter Watchdog"
#ifndef AppVersion
  #define AppVersion "3.0"
#endif

[Setup]
AppId={{6E0B7C1A-5D2F-4E8B-9A33-2B6A4F1C9D70}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Remote Printer
DefaultDirName={localappdata}\RemotePrinter
PrivilegesRequired=lowest
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
AlwaysShowComponentsList=no
CloseApplications=force
RestartApplications=no
OutputDir=..\dist
OutputBaseFilename=RemotePrinter-Setup
Compression=lzma2/max
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

[Registry]
; start hidden every time this user logs in to Windows
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "RemotePrinter"; ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue

[Run]
; keep the computer awake while on mains power, so it can always print
Filename: "{sys}\powercfg.exe"; Parameters: "/change standby-timeout-ac 0"; Flags: runhidden
Filename: "{sys}\powercfg.exe"; Parameters: "/change hibernate-timeout-ac 0"; Flags: runhidden
; watchdog: every 5 minutes make sure the program is running (a second copy exits at once)
Filename: "{sys}\schtasks.exe"; Parameters: "/Create /F /SC MINUTE /MO 5 /TN ""{#Watchdog}"" /TR ""\""{app}\{#AppExe}\"""""; Flags: runhidden
; start now - no restart needed
Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Flags: nowait runhidden

[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""{#Watchdog}"""; Flags: runhidden; RunOnceId: "DelWatchdog"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopRemotePrinter"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  // stop a running copy so its files can be replaced (settings in config.ini are kept)
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1000);
  Result := '';
end;
