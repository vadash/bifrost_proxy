@echo off
setlocal

set "LISTEN=127.0.0.1:8088"

echo Stopping Bifrost routing sidecar on http://%LISTEN% ...

set "KILLED=0"

rem --- find every PID listening on :8088 (TCP) and taskkill it ---
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /R /C:"%LISTEN% .*LISTENING"') do (
    echo [stop] killing listener PID %%P on %LISTEN%
    taskkill /F /PID %%P >nul 2>&1
    set "KILLED=1"
)

rem --- also kill any lingering `python -m sidecar` process by command line ---
for /f "tokens=2 delims=," %%K in (
    'wmic process where "name='python.exe' and CommandLine like '%%-m sidecar%%'" get ProcessId /format:csv 2^>nul ^| find /i "python.exe"'
) do (
    echo [stop] killing sidecar python PID %%K
    taskkill /F /PID %%K >nul 2>&1
    set "KILLED=1"
)

if "%KILLED%"=="0" (
    echo [INFO] No sidecar process found on %LISTEN%. Nothing to stop.
) else (
    echo [DONE] Sidecar stopped.
)

endlocal
