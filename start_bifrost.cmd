@echo off
SETLOCAL EnableDelayedExpansion

:: Define the port Bifrost uses (Default is 8080)
set "PORT=8080"

echo Checking if Bifrost AI Gateway is already running on port %PORT%...

:: Look for an active listener on the specified port
netstat -ano | findstr /R /C:":%PORT% *.* LISTENING" >nul

if %errorlevel% equ 0 (
    echo [INFO] Bifrost is already running or port %PORT% is in use. Exiting to prevent duplicate process.
    timeout /t 3 >nul
    exit /b 0
) else (
    echo [STARTING] Bifrost is not running. Launching now...
    :: -y skips the interactive installation prompt to make it fully automated
    npx -y @maximhq/bifrost --port %PORT%
)

ENDLOCAL
