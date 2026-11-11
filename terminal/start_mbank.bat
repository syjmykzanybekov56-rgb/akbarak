@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AkBarak - kanal MBank
where python >nul 2>nul || (echo Python ne naiden. Ustanovite Python s python.org & pause & exit /b)
python -c "import requests" 2>nul || python -m pip install requests
:loop
curl -fsSL -o mkassa_relay.new "https://raw.githubusercontent.com/syjmykzanybekov56-rgb/akbarak/main/terminal/mkassa_relay.py" && findstr /c:"VERSION =" mkassa_relay.new >nul && move /y mkassa_relay.new mkassa_relay.py >nul
if exist mkassa_relay.new del mkassa_relay.new
python mkassa_relay.py
echo Perezapusk cherez 10 sekund...
timeout /t 10 /nobreak >nul
goto loop
