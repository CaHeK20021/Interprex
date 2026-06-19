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
    echo  Nothing to commit.
    del "%TEMP%\gst.tmp" >nul 2>&1
    pause & exit /b 0
)
del "%TEMP%\gst.tmp" >nul 2>&1

set "MSG=update"
set /p "MSG=  Commit message (Enter = 'update'): "
if "!MSG!"=="" set "MSG=update"

git add .
git commit -m "!MSG!"
if errorlevel 1 (
    echo  Nothing new to commit.
    pause & exit /b 0
)

for /f "tokens=*" %%B in ('git branch --show-current 2^>nul') do set "BRANCH=%%B"
if "!BRANCH!"=="" set "BRANCH=main"

echo.
echo  Pushing to origin/!BRANCH!...
git push -u origin "!BRANCH!"

echo.
echo  Done!
echo.
pause
endlocal
