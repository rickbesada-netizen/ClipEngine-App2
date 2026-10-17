@echo off
setlocal
title Clip Engine
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto run

echo.
echo  First run: setting up Clip Engine. This takes a few minutes and needs internet.
echo.
set "PY="
py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
if not defined PY (py -3.11 -c "import sys" >nul 2>&1 && set "PY=py -3.11")
if not defined PY (py -3 -c "import sys; assert sys.version_info>=(3,10)" >nul 2>&1 && set "PY=py -3")
if not defined PY (python -c "import sys; assert sys.version_info>=(3,10)" >nul 2>&1 && set "PY=python")
if not defined PY (
  echo  Python is not installed. Installing Python 3.12 now...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  echo.
  echo  Python is installed. Close this window and double-click START.bat again.
  pause
  exit /b
)
%PY% -m venv .venv
if errorlevel 1 (echo  Could not create the Python environment. & pause & exit /b)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo  Install failed. Check your internet connection and run START.bat again.
  rmdir /s /q .venv
  pause
  exit /b
)
".venv\Scripts\python.exe" -m pip install deno >nul 2>&1
echo.
echo  Downloading the transcription model (about 150 MB, once)...
".venv\Scripts\python.exe" -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')"

:run
echo  Checking for downloader updates...
".venv\Scripts\python.exe" -m pip install -q -U "yt-dlp[default]"
set "PATH=%~dp0.venv\Scripts;%PATH%"
".venv\Scripts\python.exe" app\server.py
pause
