@echo off
setlocal

set "LISTEN_PORT=8088"
set "LISTEN=127.0.0.1:%LISTEN_PORT%"

rem --- pick a bind address: Tailscale IP when available, else localhost only ---
rem     Mirrors start_sidecar.cmd so stop targets the same LISTEN value.
set "TS_IP="
for /f "delims=" %%I in ('tailscale ip -4 2^>nul ^| findstr /R /C:"^[0-9]"') do set "TS_IP=%%I"
if defined TS_IP (
  set "LISTEN=%TS_IP%:%LISTEN_PORT%"
  set "TAILSCALE_MSG=Tailscale %TS_IP%"
) else (
  set "TAILSCALE_MSG=localhost only (tailscale.exe not found / no IP)"
)

echo Stopping Bifrost routing sidecar on http://%LISTEN%  ^(%TAILSCALE_MSG%^)

set "KILLED=0"

rem --- kill the listener on the resolved bind (Tailscale IP or localhost) ---
rem     Match on the exact %LISTEN% token, the way start_sidecar.cmd's guard does.
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /R /C:"%LISTEN% .*LISTENING"') do (
    echo [stop] killing listener PID %%P on %LISTEN%
    taskkill /F /PID %%P >nul 2>&1
    set "KILLED=1"
)

rem --- fallback: kill any PID listening on :%LISTEN_PORT% (any bind) ---
rem     Catches a stale bind on the other address if the IP changed since start.
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /R /C:":%LISTEN_PORT% .*LISTENING"') do (
    echo [stop] killing listener PID %%P on :%LISTEN_PORT%
    taskkill /F /PID %%P >nul 2>&1
    set "KILLED=1"
)

rem --- kill any lingering `python -m sidecar-2` process by command line ---
rem     wmic is removed on modern Windows 11; use PowerShell Get-CimInstance
rem     (the live replacement). Matches the actual command line ("-m sidecar-2").
for /f "delims=" %%K in ('powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object { $_.CommandLine -like '*-m sidecar-2*' } | Select-Object -ExpandProperty ProcessId" 2^>nul') do (
    echo [stop] killing sidecar python PID %%K
    taskkill /F /PID %%K >nul 2>&1
    set "KILLED=1"
)

if "%KILLED%"=="0" (
    echo [INFO] No sidecar process found on %LISTEN%. Nothing to stop.
) else (
    echo [DONE] Sidecar stopped.
)

rem NOTE: Tailscale itself is intentionally left up -- the sidecar is just a
rem port listener on the tailnet IP, not the node. Bringing the node down to
rem stop one app would nuke every other Tailscale-dependent thing on this host.
endlocal
