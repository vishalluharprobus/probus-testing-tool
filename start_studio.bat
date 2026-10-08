@echo off
rem Probus Test Studio - double-click to start it for the whole office.
rem Close this window (or press Ctrl+C) to stop it.
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
  echo The venv folder is missing. Set the project up first - see README / verify_setup.py.
  pause
  exit /b 1
)
venv\Scripts\python -c "import fastapi, uvicorn" 2>nul || venv\Scripts\python -m pip install -q -r requirements.txt
title Probus Test Studio
venv\Scripts\python -m portal %*
pause
