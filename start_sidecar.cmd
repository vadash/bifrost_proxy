@echo off
setlocal

set "LISTEN=127.0.0.1:8088"

echo Starting Bifrost routing sidecar on http://%LISTEN%

rem --- single-copy guard: bail if :8088 already listening ---
netstat -ano -p tcp | findstr /R /C:"%LISTEN% .*LISTENING" >nul
if not errorlevel 1 (
  echo [sidecar] already listening on %LISTEN% - not starting a second copy.
  echo Repoint your client baseUrl to http://%LISTEN%/v1  ^(Bifrost stays on :8080^)
  exit /b 0
)

echo Repoint your client baseUrl to http://%LISTEN%/v1  ^(Bifrost stays on :8080^)
cd /d "%~dp0"

rem --- rotate decision log: delete on start so it cannot overflow ---
if exist "sidecar-2\sidecar.log" del /q "sidecar-2\sidecar.log"

rem --- reserve first 3 alpha-sorted providers (nvidia-1..3) for the Bifrost
rem     auto route; sidecar pools the remaining 12 (nvidia-4..15) ---
python -m sidecar-2 --reserve-bifrost 3
