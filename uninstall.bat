@echo off
setlocal EnableExtensions

:: ============================================================================
::  BMLAPI Worker - uninstall  (stop server + remove auto-start)
:: ----------------------------------------------------------------------------
::  Double-click this file. It asks for Administrator (UAC), then:
::    1. stops and deletes the boot/auto-start task "BMLAPI Worker Server"
::    2. stops the running server (frees port 8000)
::    3. removes the Windows Firewall rule for port 8000
::
::  It does NOT delete your code, venv, logs, or this folder - only the
::  startup task, the running process, and the firewall rule.
:: ============================================================================

set "TASK=BMLAPI Worker Server"
set "FWRULE=BMLAPI Worker (port 8000) - allowed IPs"

:: --- self-elevate to Administrator ------------------------------------------
net session >nul 2>&1
if %errorlevel% NEQ 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ============================================================
echo  BMLAPI Worker - uninstall
echo ============================================================
echo.

:: --- 1. remove the auto-start task ------------------------------------------
echo [1/3] Removing auto-start task "%TASK%"...
schtasks /End    /TN "%TASK%" >nul 2>&1
schtasks /Delete /TN "%TASK%" /F >nul 2>&1
schtasks /Query  /TN "%TASK%" >nul 2>&1
if %errorlevel% EQU 0 (
    echo        WARNING: task still present - it may not have existed under that name.
) else (
    echo        Task removed (or was not present).
)

:: --- 2. stop the running server ---------------------------------------------
echo [2/3] Stopping the running server...
:: Per the project's operating notes, the server runs as python.exe; this stops it.
taskkill /F /IM python.exe >nul 2>&1
if %errorlevel% EQU 0 (
    echo        Stopped running python.exe process(es).
) else (
    echo        No running python.exe process found.
)

:: --- 3. remove the firewall rule --------------------------------------------
echo [3/3] Removing firewall rule for port 8000...
netsh advfirewall firewall delete name="%FWRULE%" >nul 2>&1
if %errorlevel% EQU 0 (
    echo        Firewall rule removed.
) else (
    echo        No matching firewall rule found.
)

:: --- verify port 8000 is no longer listening --------------------------------
echo.
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { Write-Host '  NOTE: something is still listening on port 8000.' } else { Write-Host '  Confirmed: nothing is listening on port 8000.' }"

echo.
echo ============================================================
echo  Uninstall complete. Auto-start removed; server stopped.
echo  Your files (server.py, venv, logs) are untouched.
echo  Run setup.bat again any time to reinstall.
echo ============================================================
echo.
pause
exit /b 0
