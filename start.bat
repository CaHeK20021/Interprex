@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo  Interprex launcher
echo  ------------------

if not exist "node_modules" (
    echo  Installing frontend dependencies - first run...
    call npm install
    if errorlevel 1 (
        echo  npm install failed.
        pause
        exit /b 1
    )
)

if not exist "python-core\venv\Scripts\python.exe" (
    echo  Creating Python environment - first run...
    python -m venv python-core\venv
    call python-core\venv\Scripts\python.exe -m pip install -q --upgrade pip -r python-core\requirements.txt
    if errorlevel 1 (
        echo  Python setup failed.
        pause
        exit /b 1
    )
)

echo  Starting sidecar in background...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process 'python-core\venv\Scripts\python.exe' -ArgumentList 'python-core\main.py','--reload' -RedirectStandardOutput 'python-core\sidecar.log' -RedirectStandardError 'python-core\sidecar-err.log' -PassThru | Select-Object -ExpandProperty Id | Out-File 'python-core\sidecar.pid' -Encoding ascii -NoNewline"
set /p SIDECAR_PID=<python-core\sidecar.pid
echo  Sidecar PID: !SIDECAR_PID!  (logs: python-core\sidecar.log)
echo.

echo  Launching app - first run compiles Rust, be patient...
echo.
call npm run tauri dev

echo.
echo  Stopping sidecar (PID !SIDECAR_PID!)...
taskkill /PID !SIDECAR_PID! /T /F >nul 2>&1
del python-core\sidecar.pid >nul 2>&1
echo  Done.
endlocal
