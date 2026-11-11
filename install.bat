@echo off
setlocal
cd /d "%~dp0"
echo === VORA MT5 AI Trader: install ===

set PY=
py -3.12 --version >nul 2>&1 && set PY=py -3.12
if "%PY%"=="" py -3.11 --version >nul 2>&1 && set PY=py -3.11
if "%PY%"=="" py -3.13 --version >nul 2>&1 && set PY=py -3.13
if "%PY%"=="" python --version >nul 2>&1 && set PY=python
if "%PY%"=="" (
  echo Python 3.12 not found. Install it from https://www.python.org/downloads/ ^(tick "Add to PATH"^) and run this again.
  pause & exit /b 1
)
echo Using: %PY%

if not exist .venv (
  %PY% -m venv .venv || (echo Could not create virtual environment & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || (echo Dependency install failed & pause & exit /b 1)

if not exist .env (
  copy .env.example .env >nul
  echo.
  echo Created .env. Opening it now: fill in MT5_LOGIN, MT5_PASSWORD, MT5_SERVER and GROQ_API_KEY, then save.
  notepad .env
)
echo.
echo Next: run check_setup.bat to verify MT5 and Groq, then start.bat
pause
