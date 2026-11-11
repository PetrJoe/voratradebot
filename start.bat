@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat ( echo Run install.bat first. & pause & exit /b 1 )
if not exist .env ( echo Missing .env - run install.bat first. & pause & exit /b 1 )
call .venv\Scripts\activate.bat
echo Starting VORA on http://127.0.0.1:8000  ^(close this window to stop^)
start "" /min cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000"
python run.py
pause
