@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set PYTHON=python-core\venv\Scripts\python.exe
%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo  Recreating Python venv...
    if exist "python-core\venv" rmdir /s /q python-core\venv
    py -3.14 -m venv python-core\venv
    %PYTHON% -m pip install -q --upgrade pip -r python-core\requirements.txt
    if errorlevel 1 ( echo  [ERROR] pip install failed. & exit /b 1 )
)

%PYTHON% -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo  Installing PyInstaller...
    %PYTHON% -m pip install -q pyinstaller
    if errorlevel 1 ( echo  [ERROR] PyInstaller install failed. & exit /b 1 )
)

echo  Building Python sidecar...
%PYTHON% -m PyInstaller --distpath python-core\dist --workpath python-core\build --noconfirm python-core\sidecar.spec
if errorlevel 1 ( echo  [ERROR] PyInstaller failed. & exit /b 1 )

set DIST=python-core\dist
copy /Y "%DIST%\sidecar.exe" "%DIST%\sidecar-x86_64-pc-windows-msvc.exe" >nul
echo  Sidecar built OK.
