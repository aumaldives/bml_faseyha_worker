@echo off
setlocal EnableExtensions EnableDelayedExpansion

:: ============================================================================
::  BMLAPI Worker - one-click setup  (FastAPI/uvicorn on 0.0.0.0:8000)
:: ----------------------------------------------------------------------------
::  Just double-click this file. It will ask for Administrator (UAC), then:
::    1. create the Python virtual environment (venv) if missing
::    2. install all dependencies from requirements.txt
::    3. create the Windows Firewall rule for inbound TCP 8000
::    4. register the auto-start task that runs the server at every boot
::    5. start the server now and confirm it is listening on port 8000
::
::  Re-running it is safe: it just refreshes everything in place.
::
::  SECURITY: this proxy has NO app-level auth, so access control lives in the
::  firewall. Only the IPs in ALLOWED_IPS below can reach port 8000.
::  >>> Edit ALLOWED_IPS to add/remove client IPs. Use "any" to allow everyone
::      (NOT recommended). Separate multiple IPs with commas, no spaces.
:: ============================================================================

set "ALLOWED_IPS=124.195.201.10,65.20.83.133"

:: --- self-elevate to Administrator ------------------------------------------
net session >nul 2>&1
if %errorlevel% NEQ 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

:: --- resolve paths (works wherever this folder is) --------------------------
cd /d "%~dp0"
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "VENV=%ROOT%\venv"
set "PY=%VENV%\Scripts\python.exe"
set "SERVER=%ROOT%\server.py"
set "TASK=BMLAPI Worker Server"
set "FWRULE=BMLAPI Worker (port 8000) - allowed IPs"

echo ============================================================
echo  BMLAPI Worker - setup  (port 8000)
echo  Folder : %ROOT%
echo  Allowed: %ALLOWED_IPS%
echo ============================================================
echo.

:: --- 1. virtual environment -------------------------------------------------
:: A venv copied from another machine has the OLD machine's Python path baked in
:: (pyvenv.cfg / the python.exe launcher), so it won't run here. Detect that and
:: rebuild from scratch instead of trusting that the folder merely "exists".
set "NEED_VENV="
if exist "%PY%" (
    "%PY%" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo [1/5] Existing venv is broken/from another machine - rebuilding...
        rmdir /s /q "%VENV%" 2>nul
        set "NEED_VENV=1"
    ) else (
        echo [1/5] Virtual environment already present and working.
    )
) else (
    set "NEED_VENV=1"
)

if defined NEED_VENV (
    echo [1/5] Creating virtual environment...
    set "BASEPY="
    where py     >nul 2>&1 && set "BASEPY=py -3"
    if not defined BASEPY ( where python >nul 2>&1 && set "BASEPY=python" )
    if not defined BASEPY (
        echo        ERROR: Python 3 not found on PATH. Install it from
        echo               https://www.python.org/downloads/ then re-run this file.
        echo               During install, tick "Add python.exe to PATH".
        goto :fail
    )
    !BASEPY! -m venv "%VENV%"
    if not exist "%PY%" ( echo        ERROR: venv creation failed. & goto :fail )
)

:: --- 2. dependencies --------------------------------------------------------
echo [2/5] Installing dependencies...
"%PY%" -m pip install --upgrade pip >nul 2>&1
if exist "%ROOT%\requirements.txt" (
    "%PY%" -m pip install -r "%ROOT%\requirements.txt"
) else (
    "%PY%" -m pip install fastapi uvicorn python-multipart cloudscraper requests
)
if %errorlevel% NEQ 0 ( echo        ERROR: pip install failed. & goto :fail )

:: --- 3. firewall rule (inbound TCP 8000, restricted to ALLOWED_IPS) ---------
echo [3/5] Configuring firewall rule for TCP 8000...
netsh advfirewall firewall delete name="%FWRULE%" >nul 2>&1
netsh advfirewall firewall add rule name="%FWRULE%" dir=in action=allow protocol=TCP localport=8000 remoteip=%ALLOWED_IPS% >nul
if %errorlevel% NEQ 0 ( echo        ERROR: failed to create firewall rule. & goto :fail )

:: --- 4. auto-start task (boot trigger, runs as SYSTEM, restarts on failure) -
echo [4/5] Registering auto-start task "%TASK%"...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$a=New-ScheduledTaskAction -Execute '%PY%' -Argument '%SERVER%' -WorkingDirectory '%ROOT%'; $t=New-ScheduledTaskTrigger -AtStartup; $p=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest; $s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -MultipleInstances IgnoreNew; Register-ScheduledTask -TaskName '%TASK%' -Action $a -Trigger $t -Principal $p -Settings $s -Description 'BMLAPI worker (FastAPI/uvicorn on 0.0.0.0:8000). Auto-starts at boot.' -Force | Out-Null"
if %errorlevel% NEQ 0 ( echo        ERROR: task registration failed. & goto :fail )

:: --- 5. (re)start the server ------------------------------------------------
echo [5/5] Starting server...
:: Stop any running instance so the new one owns port 8000.
:: (Per the project's operating notes, the restart kills all python.exe.)
schtasks /End /TN "%TASK%" >nul 2>&1
taskkill /F /IM python.exe >nul 2>&1
schtasks /Run /TN "%TASK%" >nul 2>&1

echo.
echo Waiting for the server to come up on port 8000...
set "UP="
for /L %%i in (1,1,20) do (
    if not defined UP (
        powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" && set "UP=1"
        if not defined UP timeout /t 1 >nul
    )
)

echo.
echo ============================================================
if defined UP (
    echo  SUCCESS - server is listening on 0.0.0.0:8000
    echo  Local tester : http://127.0.0.1:8000/docs
    echo  Logs         : %ROOT%\requests.log
    echo  It will now auto-start at every boot.
) else (
    echo  WARNING - port 8000 is not listening yet.
    echo  Check the log for errors: %ROOT%\requests.log
)
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo ************************************************************
echo  SETUP FAILED - see the messages above.
echo ************************************************************
echo.
pause
exit /b 1
