@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
echo.
echo   RunBeat 开发模式（改代码自动重载）  http://127.0.0.1:8000
echo   按 Ctrl+C 停止
echo.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
pause
