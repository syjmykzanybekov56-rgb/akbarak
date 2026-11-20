@echo off
REM AkBarak: ustanovka terminala odnim zapuskom.
REM Skachivaet setup.ps1 s GitHub i zapuskaet ego ot imeni administratora.
chcp 65001 >nul
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
set "PS1=%TEMP%\akbarak_setup.ps1"
set "URL=https://raw.githubusercontent.com/syjmykzanybekov56-rgb/akbarak/main/terminal/setup.ps1"
if exist "%PS1%" del "%PS1%"
curl -fsSL -o "%PS1%" "%URL%"
if not exist "%PS1%" powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing '%URL%' -OutFile '%PS1%'"
if not exist "%PS1%" (
  echo Ne udalos skachat ustanovshik. Proverte internet i zapustite eshe raz.
  pause
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
pause
