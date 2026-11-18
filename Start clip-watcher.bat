@echo off
title clip-watcher
cd /d "%~dp0"

rem Find Python. Captions (faster-whisper) need Python 3.12 - 3.14, so prefer those
rem even when a newer Python is installed too; then any Python 3, then "python".
set "PY="
for %%V in (3.14 3.13 3.12) do (
  if not defined PY py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
)
if not defined PY (
  py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo  Python was not found.
  echo  Install Python 3.14 (64-bit^) from https://www.python.org/downloads/windows/
  echo  and tick "Add python.exe to PATH" in the installer.
  echo  See "SETUP GUIDE.txt", Step 1.
  echo.
  pause
  exit /b 1
)

echo Starting clip-watcher... your browser opens http://localhost:8765
echo Keep this window open while clipping. Press Ctrl+C here to stop everything.
echo.
%PY% -m clipwatcher dashboard %*
echo.
echo clip-watcher has stopped.
pause
