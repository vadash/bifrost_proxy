@echo off
setlocal

rem --- restart the sidecar: stop then start in one command ---
rem     Delegates to stop_sidecar.cmd and start_sidecar.cmd so the kill +
rem     launch logic lives in exactly one place each.
rem
rem     Usage:  restart_sidecar.cmd

call "%~dp0stop_sidecar.cmd"
if errorlevel 1 (
    echo [restart] stop returned an error - aborting restart.
    exit /b 1
)

call "%~dp0start_sidecar.cmd"
if errorlevel 1 (
    echo [restart] start returned an error - sidecar may be down.
    exit /b 1
)

endlocal
