@echo off
rem ---------------------------------------------------------------------------
rem RunBeat launcher (browser mode).
rem
rem IMPORTANT: keep this file pure ASCII.
rem cmd.exe parses .bat files byte-by-byte using the OEM code page (GBK on a
rem Chinese Windows), so UTF-8 Chinese in an `echo` line gets mis-decoded and
rem its fragments get executed as commands. `chcp 65001` only changes console
rem OUTPUT encoding, not how the batch file itself is parsed -- it cannot fix it.
rem The Chinese "starting..." banner lives in app/banner.py instead.
rem ---------------------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

".venv\Scripts\python.exe" -m app.banner
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

pause
