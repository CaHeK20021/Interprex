@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo  ==========================================
echo   Interprex — Full Build
echo  ==========================================
echo.

:: --- 1. Check Python 3.14 -------------------------------------------------
py -3.14 --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python 3.14 not found.
    echo  Download: https://www.python.org/downloads/
    pause & exit /b 1
)

:: --- 2. Recreate venv if broken -------------------------------------------
set PYTHON=python-core\venv\Scripts\python.exe
%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo  Recreating Python venv...
    if exist "python-core\venv" rmdir /s /q python-core\venv
    py -3.14 -m venv python-core\venv
    %PYTHON% -m pip install -q --upgrade pip -r python-core\requirements.txt
    if errorlevel 1 ( echo  [ERROR] pip install failed. & pause & exit /b 1 )
    echo  Venv ready.
)

:: --- 3. Install PyInstaller if missing ------------------------------------
%PYTHON% -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo  Installing PyInstaller...
    %PYTHON% -m pip install -q pyinstaller
    if errorlevel 1 ( echo  [ERROR] PyInstaller install failed. & pause & exit /b 1 )
)

:: --- 4. Build Python sidecar ----------------------------------------------
echo  Building UAssetExtractor (C#)...
dotnet build python-core\uasset-extractor\UAssetExtractor.csproj -c Release -o python-core\bin
if errorlevel 1 ( echo  [ERROR] Dotnet build for UAssetExtractor failed. & pause & exit /b 1 )

echo  [1/2] Building Python sidecar...
%PYTHON% -m PyInstaller --distpath python-core\dist --workpath python-core\build --noconfirm python-core\sidecar.spec
if errorlevel 1 ( echo  [ERROR] PyInstaller failed. & pause & exit /b 1 )

set DIST=python-core\dist
copy /Y "%DIST%\sidecar.exe" "%DIST%\sidecar-x86_64-pc-windows-msvc.exe" >nul
echo  Sidecar built OK.
echo.

:: --- 5. Build Tauri app ---------------------------------------------------
echo  [2/2] Building Tauri app (Rust compiles on first run - ~10 min)...
call npm run tauri build
if errorlevel 1 ( echo  [ERROR] Tauri build failed. & pause & exit /b 1 )

:: --- 6. Done --------------------------------------------------------------
echo.
echo  ==========================================
echo   BUILD COMPLETE
echo  ==========================================
echo.
echo  Installer is in:
echo    src-tauri\target\release\bundle\msi\
echo    src-tauri\target\release\bundle\nsis\
echo.
explorer src-tauri\target\release\bundle
pause
