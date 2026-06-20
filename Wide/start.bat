@echo off
title Fortnite Ranked Overlay - Launcher
cd /d "%~dp0"

echo ============================================
echo      Fortnite Ranked Overlay Launcher
echo ============================================
echo.

:: 1. Explicitly check if the python file exists
if not exist "server.py" (
    echo [ERROR] Could not find 'server.py' in this folder!
    echo Please ensure that you saved your Python code exactly as 'server.py'.
    echo.
    pause
    exit /b 1
)

:: 2. Check if port 8888 is already active
netstat -aon | findstr ":8888 " >nul 2>nul
if %errorlevel% neq 0 goto start_server

:: If the script reaches this point, the port is occupied
echo [WARNING] The overlay is already running (Port 8888 is occupied).
set "CHOICE="
set /p "CHOICE=Would you like to force close it and restart? (Y/N): "

if /i "%CHOICE%"=="N" (
    echo.
    echo Okay, keeping the existing overlay running. Closing console...
    timeout /t 2 /nobreak >nul
    exit /b 0
)

:: If they typed Y (or anything else), execute the aggressive force-close
echo.
echo [INFO] Executing aggressive force-close...

:: Kill whatever is on port 8888 along with its entire process tree (/T)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8888 "') do (
    taskkill /F /PID %%a /T >nul 2>nul
)

:: Also snipe pythonw.exe globally just in case it detached from the port
taskkill /F /IM pythonw.exe /T >nul 2>nul

echo [INFO] Overlay processes successfully terminated.
echo [INFO] Waiting 5 seconds before automatically restarting...
timeout /t 5 /nobreak >nul
echo.

:start_server
:: 3. Locate Python / Pythonw cleanly
set "PYW="
where pythonw >nul 2>nul && set "PYW=pythonw"

if not defined PYW (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PYW (
            if exist "%%~dpipythonw.exe" set "PYW=%%~dpipythonw.exe"
        )
    )
)

if not defined PYW (
    where python >nul 2>nul && set "PYW=python"
)

if not defined PYW (
    echo [ERROR] Python was not detected on this system.
    echo Please install Python 3 from https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo [SUCCESS] Found Python environment. Starting overlay...
echo OBS Browser Source URL: http://localhost:8888/overlay
echo.

:: 4. Smart Launching
if "%PYW%"=="python" (
    start "Fortnite Overlay Server" python "server.py"
) else (
    start "" "%PYW%" "server.py"
)

:: 5. Open up a preview tab automatically
start "" cmd /c "timeout /t 2 /nobreak >nul & start "" http://localhost:8888/overlay"

echo [SUCCESS] Launcher finishing up. This window will auto-close.
timeout /t 3 /nobreak >nul