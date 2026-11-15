@echo off
rem ---------------------------------------------------------------------------
rem RunBeat launcher (dev mode: auto-reload on code change).
rem
rem IMPORTANT: keep this file pure ASCII -- see the note in run.bat.
rem ---------------------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

".venv\Scripts\python.exe" -m app.banner dev
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

pause
