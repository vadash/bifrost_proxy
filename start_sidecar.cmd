@echo off
echo Starting Bifrost capture sidecar on http://127.0.0.1:8088
echo Repoint your client baseUrl to http://127.0.0.1:8088/v1  (Bifrost stays on :8080)
python "%~dp0sidecar\proxy.py"
