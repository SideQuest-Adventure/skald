@echo off
echo Installing Skald dependencies...
pip install faster-whisper sounddevice numpy pyperclip pynput pyautogui scipy
echo.
echo Done. The Whisper model (small.en, ~244 MB) downloads automatically on first run.
echo Start with run.bat.  Check your setup any time with:  python skald.py --doctor
pause
