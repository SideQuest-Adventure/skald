@echo off
cd /d "%~dp0"
echo Starting Skald - CLASSIC hold-to-talk mode (no window)...
echo Hold Right Ctrl to record, release to transcribe and paste.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch.ps1" -Classic %*
pause
