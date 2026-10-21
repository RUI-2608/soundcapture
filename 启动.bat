@echo off
chcp 65001 >nul
if exist "%~dp0dist\SoundCapture\SoundCapture.exe" (
  start "" "%~dp0dist\SoundCapture\SoundCapture.exe"
) else (
  set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\tkgui\Scripts\pythonw.exe"
  if not exist "%PY%" set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\tkgui\Scripts\python.exe"
  start "" "%PY%" "%~dp0SoundCapture.py"
)
