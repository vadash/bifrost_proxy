@echo off
SETLOCAL EnableDelayedExpansion

::: Default Bifrost port
set "PORT=8080"

echo Stopping Bifrost AI Gateway on port %PORT% ...

set "KILLED=0"

rem --- find every PID listening on %PORT% (TCP) and taskkill it ---
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    echo [stop] killing listener PID %%P on port %PORT%
    taskkill /F /PID %%P >nul 2>&1
    set "KILLED=1"
)

rem --- also kill any lingering @maximhq/bifrost node process by command line ---
for /f "tokens=2 delims=," %%K in (
    'wmic process where "name='node.exe' and CommandLine like '%%@maximhq/bifrost%%'" get ProcessId /format:csv 2^>nul ^| find /i "node.exe"'
) do (
    echo [stop] killing bifrost node PID %%K
    taskkill /F /PID %%K >nul 2>&1
    set "KILLED=1"
)

if "!KILLED!"=="0" (
    echo [INFO] No Bifrost process found on port %PORT%. Nothing to stop.
) else (
    echo [DONE] Bifrost stopped.
)

ENDLOCAL
