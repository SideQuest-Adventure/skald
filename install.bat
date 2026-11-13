@echo off
echo Installing Skald into its own Python environment...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch.ps1" -Install
if errorlevel 1 exit /b 1
pause
