@echo off
REM АкБарак: запуск авто-печати с автоперезапуском при сбое
chcp 65001 >nul
cd /d "%~dp0"
:loop
REM Перед запуском скачиваем свежую версию воркера с GitHub (если нет интернета — работает старая)
curl -fsSL -o print_worker.new "https://raw.githubusercontent.com/syjmykzanybekov56-rgb/akbarak/main/terminal/print_worker.py" && findstr /c:"VERSION =" print_worker.new >nul && move /y print_worker.new print_worker.py >nul
if exist print_worker.new del print_worker.new
python print_worker.py
echo Воркер остановился, перезапуск через 10 секунд...
timeout /t 10 /nobreak >nul
goto loop
