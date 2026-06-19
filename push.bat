@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo  ================================
echo   Interprex  ^|  GitHub push
echo  ================================
echo.

where git >nul 2>&1
if errorlevel 1 (
    echo  [ERR] Git not found. Install from https://git-scm.com
    pause & exit /b 1
)

if not exist ".git" (
    echo  Initializing repository...
    git init
    git branch -M main
    echo.
    set /p "REMOTE=  GitHub repository URL (https://github.com/user/repo.git): "
    if "!REMOTE!"=="" (
        echo  [ERR] No URL entered. Create a repo on GitHub and run again.
        pause & exit /b 1
    )
    git remote add origin "!REMOTE!"
    echo.
)

git remote get-url origin >nul 2>&1
if errorlevel 1 (
    echo  Remote 'origin' not configured.
    set /p "REMOTE=  GitHub repository URL: "
    if "!REMOTE!"=="" (
        echo  [ERR] No URL entered.
        pause & exit /b 1
    )
    git remote add origin "!REMOTE!"
    echo.
)

echo  Status:
git status --short
echo.

git status --porcelain > "%TEMP%\gst.tmp" 2>&1
for %%A in ("%TEMP%\gst.tmp") do if %%~zA==0 (
    echo  No code changes.
    del "%TEMP%\gst.tmp" >nul 2>&1
    goto :skip_commit
)
del "%TEMP%\gst.tmp" >nul 2>&1

set "MSG=update"
set /p "MSG=  Commit message (Enter = 'update'): "
if "!MSG!"=="" set "MSG=update"

git add .
git commit -m "!MSG!"

:skip_commit

for /f "tokens=*" %%B in ('git branch --show-current 2^>nul') do set "BRANCH=%%B"
if "!BRANCH!"=="" set "BRANCH=main"

echo.
echo  Pushing to origin/!BRANCH!...
git push -u origin "!BRANCH!"

echo.
echo  Bumping version and tagging...

:: Read current version from package.json via PowerShell
for /f %%V in ('powershell -Command "(Get-Content 'package.json' -Raw | ConvertFrom-Json).version"') do set "CURVER=%%V"
if "!CURVER!"=="" (
    echo  [WARN] Could not read version from package.json, skipping tag.
    echo.
    echo  Done!
    echo.
    pause
    endlocal
    exit /b 0
)

:: Parse MAJOR.MINOR.PATCH
for /f "tokens=1-3 delims=." %%A in ("!CURVER!") do (
    set "MAJ=%%A"
    set "MIN=%%B"
    set "PAT=%%C"
)
if "!PAT!"=="" set "PAT=0"
set /a "NEWPAT=!PAT!+1"
set "NEWVER=!MAJ!.!MIN!.!NEWPAT!"
set "NEWTAG=v!NEWVER!"

echo  !CURVER! --^> !NEWVER!

:: Update all versions (Python preserves Cargo.toml formatting)
python set_version000.py set !NEWVER!

:: Commit version bump and push
git add package.json src-tauri/tauri.conf.json src-tauri/Cargo.toml
git commit -m "v!NEWVER!"
git push origin "!BRANCH!"

:: Tag and push
git tag !NEWTAG!
git push origin !NEWTAG!

echo.
echo  Done! Tagged !NEWTAG!
echo.
pause
endlocal
