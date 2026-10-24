@echo off
REM АкБарак: запуск авто-печати с автоперезапуском при сбое
chcp 65001 >nul
cd /d "%~dp0"
:loop
python print_worker.py
echo Воркер остановился, перезапуск через 10 секунд...
timeout /t 10 /nobreak >nul
goto loop
