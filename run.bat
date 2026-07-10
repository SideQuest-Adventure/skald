@echo off
cd /d "%~dp0"
echo Starting Skald (LIVE mode is the default)...
echo Tap Right Ctrl to start/stop; the floating overlay shows the waveform and words.
echo Want classic hold-to-talk with no window? Use run-classic.bat.
echo Tip: run  python skald.py --doctor  to check your setup, or --list to pick a mic.
python skald.py
pause
