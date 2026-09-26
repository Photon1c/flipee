@echo off
REM ---------------------------------------------------------------------
REM  Flipee relay firewall rule -- RIGHT-CLICK THIS FILE and choose
REM  "Run as administrator".
REM
REM  This is the reliable way to do it on Windows: .ps1 files have no
REM  "Run as administrator" item in their right-click menu, and having a
REM  script elevate itself opens a second window that vanishes on any
REM  error. Elevating the .cmd first means everything runs in one visible
REM  window that stays open.
REM
REM  Pass "remove" as an argument to delete the rule instead.
REM ---------------------------------------------------------------------

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   This needs administrator rights.
    echo.
    echo   Right-click "allow-relay-firewall.cmd" and choose
    echo   "Run as administrator", then try again.
    echo.
    pause
    exit /b 1
)

if /i "%~1"=="remove" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0allow-relay-firewall.ps1" -Remove
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0allow-relay-firewall.ps1"
)

exit /b %errorlevel%
