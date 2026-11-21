@echo off
REM АкБарак: экран терминала в режиме киоска (во весь экран, без панелей браузера).
REM Если экран закрыть (свайпом, Alt+F4, сбоем) — через 3 секунды он откроется снова:
REM студент не выйдет на рабочий стол Windows.
REM Чтобы свернуть экран для настройки: админка -> «Свернуть экран» (или создать файл kiosk_off.txt рядом).
REM Вернуть: админка -> «Вернуть экран» (или удалить kiosk_off.txt).
REM Номер терминала берётся из переменной AKBARAK_TERMINAL (её задаёт setup.bat), иначе kgma-1
chcp 65001 >nul
cd /d "%~dp0"
if not defined AKBARAK_TERMINAL set AKBARAK_TERMINAL=kgma-1
set "URL=https://akbarak.vercel.app/kiosk.html?t=%AKBARAK_TERMINAL%"
set "PROFILE=%~dp0chrome-kiosk"
set "FLAGS=--kiosk --noerrdialogs --disable-pinch --overscroll-history-navigation=0 --disable-session-crashed-bubble --no-first-run --disable-translate --disable-features=Translate,TouchpadOverscrollHistoryNavigation --user-data-dir=%PROFILE%"

set "BROWSER="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "BROWSER=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER (
  set "BROWSER=msedge"
  set "FLAGS=%FLAGS% --edge-kiosk-type=fullscreen"
)

:loop
if exist "%~dp0kiosk_off.txt" (
  timeout /t 5 /nobreak >nul
  goto loop
)
start "" /wait "%BROWSER%" %FLAGS% "%URL%"
timeout /t 3 /nobreak >nul
goto loop
