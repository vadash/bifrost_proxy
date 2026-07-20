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
python "%~dp0sidecar\proxy.py"
