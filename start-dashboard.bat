@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   Index Dashboard - local server
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo Please install Python 3 and make sure "python" works in cmd.
    pause
    exit /b 1
)

rem 2026-11-27: prefer the project venv. The bundled WorkBuddy interpreter had its
rem site-packages gutted and every refresh failed with
rem "No module named 'pandas.io.formats.csvs'".
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo Opening http://127.0.0.1:8765/
echo Keep this window open while using the dashboard.
echo Press Ctrl+C to stop.
echo.

start "" http://127.0.0.1:8765/
"%PY%" scripts\server.py

echo.
echo Server stopped.
pause
