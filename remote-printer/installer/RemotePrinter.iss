; Remote Printer - Windows installer (Inno Setup 6)
; Everything is inside: the program, Python runtime, libraries and SumatraPDF.
; Installs per user (no administrator rights), starts hidden at every logon.

#define AppName "הדפסה מרחוק"
#define AppExe "RemotePrinter.exe"
#define SiteUrl "https://motiml77.github.io/NAO-back-pack-face-recognitaion/"
#ifndef AppVersion
  #define AppVersion "2.0"
#endif

[Setup]
AppId={{6E0B7C1A-5D2F-4E8B-9A33-2B6A4F1C9D70}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Remote Printer
DefaultDirName={localappdata}\RemotePrinter
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
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

[Tasks]
Name: "nosleep"; Description: "למנוע מהמחשב להיכנס למצב שינה כשהוא מחובר לחשמל (מומלץ, כדי שתמיד יוכל להדפיס)"

[Files]
Source: "..\dist\RemotePrinter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "SumatraPDF.exe"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
; start hidden every time this user logs in to Windows
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "RemotePrinter"; ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue

[Run]
Filename: "{sys}\powercfg.exe"; Parameters: "/change standby-timeout-ac 0"; Flags: runhidden; Tasks: nosleep
Filename: "{sys}\powercfg.exe"; Parameters: "/change hibernate-timeout-ac 0"; Flags: runhidden; Tasks: nosleep
Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Flags: nowait runhidden
Filename: "{#SiteUrl}"; Description: "לפתוח את אתר ההדפסה"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopRemotePrinter"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
var
  PasswordPage: TInputQueryWizardPage;

function HasExistingConfig: Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\config.ini'));
end;

function ChosenPassword: String;
begin
  if WizardSilent then
    Result := Trim(ExpandConstant('{param:PASSWORD|}'))
  else
    Result := Trim(PasswordPage.Values[0]);
end;

procedure InitializeWizard;
var
  Info: String;
begin
  Info := 'בעזרת הסיסמה הזאת נכנסים לאתר ההדפסה מכל טלפון או מחשב.' + #13#10 +
          'רק מי שיודע את הסיסמה יכול לשלוח הדפסות למחשב הזה. בחרו סיסמה של 8 תווים לפחות, ושמרו אותה.';
  if HasExistingConfig then
    Info := Info + #13#10#13#10 + 'התוכנה כבר מותקנת: אפשר להשאיר ריק כדי לשמור את הסיסמה הקיימת.';
  PasswordPage := CreateInputQueryPage(wpWelcome, 'סיסמה לאתר ההדפסה', 'בחירת סיסמה', Info);
  PasswordPage.Add('סיסמה:', True);
  PasswordPage.Add('הקלידו שוב את הסיסמה:', True);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Pw: String;
begin
  Result := True;
  if (not WizardSilent) and (CurPageID = PasswordPage.ID) then
  begin
    Pw := Trim(PasswordPage.Values[0]);
    if (Pw = '') and HasExistingConfig then
      Exit;
    if Length(Pw) < 8 then
    begin
      MsgBox('הסיסמה צריכה להיות באורך 8 תווים לפחות.', mbError, MB_OK);
      Result := False;
    end
    else if Pw <> Trim(PasswordPage.Values[1]) then
    begin
      MsgBox('הסיסמאות לא תואמות. הקלידו שוב.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  // stop a running copy so its files can be replaced
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1000);
  Result := '';
  if WizardSilent and (ChosenPassword = '') and (not HasExistingConfig) then
    Result := 'Silent install needs /PASSWORD=...';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Lines: TArrayOfString;
begin
  if (CurStep = ssPostInstall) and (ChosenPassword <> '') then
  begin
    // The program replaces the password with derived keys the first time it starts.
    SetArrayLength(Lines, 2);
    Lines[0] := '[general]';
    Lines[1] := 'password = ' + ChosenPassword;
    SaveStringsToUTF8File(ExpandConstant('{app}\config.ini'), Lines, False);
    DeleteFile(ExpandConstant('{app}\state.json'));
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
    WizardForm.FinishedLabel.Caption :=
      'ההתקנה הסתיימה, והתוכנה כבר פועלת ברקע.' + #13#10 +
      'היא תעלה לבד בכל הפעלה של המחשב, בלי חלונות.' + #13#10#13#10 +
      'אתר ההדפסה (שמרו אותו במועדפים בטלפון):' + #13#10 + '{#SiteUrl}' + #13#10#13#10 +
      'נכנסים עם הסיסמה שבחרתם, כותבים שם או בוחרים קובץ, ושולחים להדפסה.';
end;
