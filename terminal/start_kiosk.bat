@echo off
REM АкБарак: экран терминала в режиме киоска (во весь экран, без панелей браузера).
REM Выйти из режима киоска: Alt+F4.
REM Номер терминала меняется в адресе: ?t=kgma-1
chcp 65001 >nul
set URL=https://akbarak.vercel.app/kiosk.html?t=kgma-1

where chrome >nul 2>nul && (start "" chrome --kiosk "%URL%" --noerrdialogs --disable-pinch --overscroll-history-navigation=0 --disable-session-crashed-bubble & goto :eof)
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (start "" "%ProgramFiles%\Google\Chrome\Application\chrome.exe" --kiosk "%URL%" --noerrdialogs --disable-pinch --overscroll-history-navigation=0 --disable-session-crashed-bubble & goto :eof)
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" (start "" "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" --kiosk "%URL%" --noerrdialogs --disable-pinch --overscroll-history-navigation=0 --disable-session-crashed-bubble & goto :eof)
start "" msedge --kiosk "%URL%" --edge-kiosk-type=fullscreen --no-first-run
