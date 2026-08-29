@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ========================================
echo  Interprex - release build
echo ========================================
echo.

where cargo >nul 2>&1
if errorlevel 1 (
    echo ERROR: cargo not found.
    echo Install Rust from https://rustup.rs
    echo.
    pause
    exit /b 1
)

if not exist "src-tauri\Cargo.toml" (
    echo ERROR: src-tauri\Cargo.toml not found.
    echo.
    pause
    exit /b 1
)

cd /d "%~dp0src-tauri"

echo [1/2] cargo tauri build --no-bundle
cargo tauri build --no-bundle
if errorlevel 1 (
    echo ERROR: build failed.
    echo.
    pause
    exit /b 1
)

set "EXE="
if defined CARGO_TARGET_DIR if exist "%CARGO_TARGET_DIR%\release\interprex.exe" set "EXE=%CARGO_TARGET_DIR%\release\interprex.exe"
if not defined EXE if exist "%LOCALAPPDATA%\cargo-target\release\interprex.exe" set "EXE=%LOCALAPPDATA%\cargo-target\release\interprex.exe"
if not defined EXE if exist "target\release\interprex.exe" set "EXE=%CD%\target\release\interprex.exe"

if not defined EXE (
    echo ERROR: interprex.exe not found after build.
    echo.
    pause
    exit /b 1
)

echo.
echo [2/2] copy exe
copy /Y "%EXE%" "%~dp0interprex.exe" >nul
if errorlevel 1 (
    echo ERROR: copy failed.
    echo.
    pause
    exit /b 1
)

echo.
echo OK
echo   %~dp0interprex.exe
echo.
pause
exit /b 0
