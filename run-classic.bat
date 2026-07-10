@echo off
cd /d "%~dp0"
echo Starting Skald - CLASSIC hold-to-talk mode (no window)...
echo Hold Right Ctrl to record, release to transcribe and paste.
python skald.py --classic
pause
