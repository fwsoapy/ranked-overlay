@echo off
title Fortnite Ranked Overlay - Setup
cd /d "%~dp0"

echo ============================================
echo      Fortnite Ranked Overlay Setup
echo ============================================
echo.
echo Pick a design:
echo.
echo   1. Minimal
echo   2. Classic
echo   3. Sharp
echo   4. Wide
echo   5. Slash
echo   6. Rainbow
echo   7. Modern
echo   8. Pulse
echo.

set "CHOICE="
set /p "CHOICE=Enter a number (1-8): "

if "%CHOICE%"=="1" set "DESIGN=Minimal"
if "%CHOICE%"=="2" set "DESIGN=Classic"
if "%CHOICE%"=="3" set "DESIGN=Sharp"
if "%CHOICE%"=="4" set "DESIGN=Wide"
if "%CHOICE%"=="5" set "DESIGN=Slash"
if "%CHOICE%"=="6" set "DESIGN=Rainbow"
if "%CHOICE%"=="7" set "DESIGN=Modern"
if "%CHOICE%"=="8" set "DESIGN=Pulse"

if not defined DESIGN (
    echo.
    echo That is not one of the options. Run setup.bat again and pick 1-8.
    echo.
    pause
    exit /b 1
)

if not exist "%DESIGN%" (
    echo.
    echo [ERROR] Could not find the "%DESIGN%" folder next to this script.
    echo Make sure setup.bat is still inside the unzipped repo folder.
    echo.
    pause
    exit /b 1
)

set "DEST=%USERPROFILE%\Desktop\Fortnite Overlay - %DESIGN%"

if exist "%DEST%" (
    echo.
    echo "%DEST%" already exists.
    set "OVERWRITE="
    set /p "OVERWRITE=Overwrite it? (Y/N): "
    if /i not "%OVERWRITE%"=="Y" (
        echo.
        echo Cancelled.
        pause
        exit /b 0
    )
    rmdir /s /q "%DEST%"
)

echo.
echo Copying %DESIGN% to your Desktop...
xcopy /E /I /Q "%DESIGN%" "%DEST%" >nul

echo.
echo [SUCCESS] Done. Open this folder on your Desktop:
echo   %DEST%
echo.
echo Inside it:
echo   1. Run account-id.bat to find your Epic Account ID
echo   2. Add it to server.py
echo   3. Run start.bat
echo.
start "" explorer "%DEST%"
pause
