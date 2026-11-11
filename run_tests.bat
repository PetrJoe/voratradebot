@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements-dev.txt
python -m pytest
pause
