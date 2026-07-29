@echo off
setlocal

set "LISTEN_PORT=8088"
set "UPSTREAM=127.0.0.1:8080"
set "LISTEN=127.0.0.1:%LISTEN_PORT%"

rem --- pick a bind address: Tailscale IP when available, else localhost only ---
set "TS_IP="
for /f "delims=" %%I in ('tailscale ip -4 2^>nul ^| findstr /R /C:"^[0-9]"') do set "TS_IP=%%I"
if defined TS_IP (
  set "LISTEN=%TS_IP%:%LISTEN_PORT%"
  set "TAILSCALE_MSG=Tailscale %TS_IP%"
) else (
  set "TAILSCALE_MSG=localhost only (tailscale.exe not found / no IP)"
)

echo Starting Bifrost routing sidecar on http://%LISTEN%  ^(%TAILSCALE_MSG%^)

rem --- single-copy guard: bail if :%LISTEN_PORT% already listening on our bind ---
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
python -m sidecar-2 --listen %LISTEN% --upstream %UPSTREAM% --reserve-bifrost 3 --cors
