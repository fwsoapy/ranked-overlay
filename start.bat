@echo off
title Fortnite Ranked Overlay - Starting...
cd /d "%~dp0"

netstat -aon | findstr ":8888 " >nul 2>nul
if %errorlevel%==0 (
    echo.
    echo  The overlay is already running.
    echo.
    set /p "CHOICE=  Would you like to terminate it and reopen it? (Y/N): "
    if /i "%CHOICE%"=="Y" (
        echo.
        echo  Stopping existing overlay...
        for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8888 "') do (
            taskkill /F /PID %%a >nul 2>nul
        )
        taskkill /F /FI "IMAGENAME eq pythonw.exe" >nul 2>nul
        timeout /t 1 /nobreak >nul
        echo  Done. Restarting...
        echo.
    ) else (
        echo.
        echo  Keeping existing overlay running. Closing.
        timeout /t 2 /nobreak >nul
        exit /b 0
    )
)

set "PYW="
where pythonw >nul 2>nul && set "PYW=pythonw"
if not defined PYW (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PYW (
            if exist "%%~dpiPythonw.exe" set "PYW=%%~dpiPythonw.exe"
        )
    )
)
if not defined PYW (
    echo.
    echo  Python was not found on this PC.
    echo  Install Python 3 from https://www.python.org/downloads/
    echo  IMPORTANT: during setup, tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo.
echo   Fortnite Ranked Overlay
echo.
echo   In OBS, add a Browser Source pointed at:
echo.
echo        http://localhost:8888/overlay
echo.
echo   If "localhost" does not connect, use http://127.0.0.1:8888/overlay
echo.
echo   The overlay is now running in the background.
echo   Run STOP_OVERLAY.bat to stop it.
echo.

start "" cmd /c "timeout /t 3 /nobreak >nul & start "" http://localhost:8888/overlay"
start "" "%PYW%" "%~dp0server.py"

echo  Overlay started silently in the background.
echo  This window will close automatically.
timeout /t 3 /nobreak >nul
