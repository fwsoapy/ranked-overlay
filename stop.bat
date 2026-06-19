@echo off
title Fortnite Ranked Overlay - Stopping...

echo.
echo  Stopping Fortnite Ranked Overlay...
echo.

REM ---- kill any pythonw process running server.py ----
taskkill /F /FI "IMAGENAME eq pythonw.exe" >nul 2>nul

REM ---- also handle if it was launched via py.exe -W ----
REM  (py.exe spawns a pythonw child; killing by window title or port is safer)
REM  Find and kill whatever is holding port 8888
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8888 "') do (
    taskkill /F /PID %%a >nul 2>nul
)

echo  Overlay stopped.
echo.
timeout /t 2 /nobreak >nul
