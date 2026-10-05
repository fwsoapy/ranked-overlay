@echo off
setlocal enabledelayedexpansion
title Fortnite Ranked Overlay
cd /d "%~dp0"

:: Read the port out of config.json so everything here follows a changed port.
set "PORT=8888"
if exist "config.json" (
    for /f "tokens=2 delims=:," %%a in ('findstr /i /c:"\"port\"" "config.json"') do (
        set "FOUND=%%a"
        set "FOUND=!FOUND: =!"
        if not "!FOUND!"=="" set "PORT=!FOUND!"
    )
)

:menu
cls
echo ============================================
echo         Fortnite Ranked Overlay
echo ============================================
echo.
call :is_running
if defined RUNNING (
    echo   Status: running on port !PORT!
) else (
    echo   Status: stopped
)
echo.
echo   1. Start the overlay
echo   2. Stop the overlay
echo   3. Find my Epic Account ID
echo   4. Edit my settings
echo   5. Quit
echo.

set "CHOICE="
set /p "CHOICE=Pick a number (1-5): "

if "%CHOICE%"=="1" goto start_overlay
if "%CHOICE%"=="2" goto stop_overlay
if "%CHOICE%"=="3" goto find_id
if "%CHOICE%"=="4" goto edit_settings
if "%CHOICE%"=="5" exit /b 0
goto menu


:: ---------------------------------------------------------------- start
:start_overlay
echo.
if not exist "server.py" (
    echo [ERROR] server.py is not in this folder. Re-download the overlay.
    pause
    goto menu
)

call :is_running
if defined RUNNING (
    echo The overlay is already running on port !PORT!.
    set "AGAIN="
    set /p "AGAIN=Restart it? (Y/N): "
    if /i not "!AGAIN!"=="Y" goto menu
    call :kill_port
)

call :find_python
if not defined PYW (
    echo [ERROR] Python is not installed.
    echo Get it from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH" during setup.
    echo.
    pause
    goto menu
)

echo Starting...
if "!PYW!"=="python" (
    start "Fortnite Overlay Server" python "server.py"
) else (
    start "" "!PYW!" "server.py"
)

echo.
echo   OBS Browser Source:  http://localhost:!PORT!/overlay
echo.
echo If an update is out you'll get a pop-up asking about it.
echo Opening a preview in your browser...
start "" cmd /c "timeout /t 2 /nobreak >nul & start "" http://localhost:!PORT!/overlay"
timeout /t 3 /nobreak >nul
goto menu


:: ----------------------------------------------------------------- stop
:stop_overlay
echo.
call :is_running
if not defined RUNNING (
    echo Nothing is running on port !PORT!.
    timeout /t 2 /nobreak >nul
    goto menu
)
call :kill_port
echo Stopped. Port !PORT! is free.
timeout /t 2 /nobreak >nul
goto menu


:: ------------------------------------------------------------ account id
:find_id
echo.
set /p "USERNAME=Enter your Epic display name: "
if "%USERNAME%"=="" goto menu

echo.
echo Looking up %USERNAME%...
echo.

powershell -NoProfile -Command ^
  "$key = '5944cf9e101f8c722009a2dd790e705295555503d544144bfcd312af2eb0fa87'; $name = [uri]::EscapeDataString('%USERNAME%'); $headers = @{'x-api-key'=$key}; $urls = @(\"https://prod.api-fortnite.com/api/v1/account/displayName/$name\", \"https://prod.api-fortnite.com/api/v1/profile/progress?displayName=$name\", \"https://prod.api-fortnite.com/api/v1/profile/stats?displayName=$name\"); $found = $false; foreach ($url in $urls) { try { $r = Invoke-RestMethod -Uri $url -Headers $headers -ErrorAction Stop; $id = $r.accountId; if (-not $id) { $id = $r.account_id }; if (-not $id) { $id = $r.id }; if (-not $id -and $r.data) { $id = $r.data.accountId }; if (-not $id -and $r.data) { $id = $r.data.account_id }; if ($id) { Write-Host \"  Display name : %USERNAME%\"; Write-Host \"  Account ID   : $id\"; $id ^| Set-Clipboard; Write-Host ''; Write-Host '  Copied to your clipboard.'; Write-Host '  Paste it into epic_account_id in config.json (option 4).'; $found = $true; break } } catch { } }; if (-not $found) { Write-Host '  No account found with that name.'; Write-Host '  Check the spelling, or look it up at https://olitracker.com'; }"

echo.
pause
goto menu


:: -------------------------------------------------------------- settings
:edit_settings
if not exist "config.json" (
    echo.
    echo config.json isn't here yet. Start the overlay once and it'll appear.
    timeout /t 3 /nobreak >nul
    goto menu
)
start "" notepad "config.json"
echo.
echo Opened config.json in Notepad. Save it, then restart the overlay
echo (option 2, then option 1) for the changes to take effect.
timeout /t 4 /nobreak >nul
goto menu


:: -------------------------------------------------------------- helpers
:is_running
set "RUNNING="
netstat -aon | findstr /r /c:":!PORT! .*LISTENING" >nul 2>nul
if not errorlevel 1 set "RUNNING=1"
exit /b

:kill_port
:: Only ever kill what is listening on our port. Killing pythonw.exe by name
:: would take down every other Python program on the machine as well.
for /f "tokens=5" %%a in ('netstat -aon ^| findstr /r /c:":!PORT! .*LISTENING"') do (
    if not "%%a"=="0" taskkill /F /PID %%a /T >nul 2>nul
)
for /l %%i in (1,1,10) do (
    netstat -aon | findstr /r /c:":!PORT! .*LISTENING" >nul 2>nul
    if errorlevel 1 exit /b
    timeout /t 1 /nobreak >nul
)
exit /b

:find_python
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
exit /b
