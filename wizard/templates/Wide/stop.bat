@echo off
title Fortnite Ranked Overlay - Stopping...

echo ============================================
echo      Stopping Fortnite Ranked Overlay
echo ============================================
echo.

echo [INFO] Releasing Port 8888...
:: Target whatever is using port 8888 and violently kill its entire process tree (/T)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8888 "') do (
    taskkill /F /PID %%a /T >nul 2>nul
)

echo [INFO] Cleaning up remaining background processes...
:: Snipe pythonw.exe globally with tree kill just in case it detached
taskkill /F /IM pythonw.exe /T >nul 2>nul

:: Catch any visible python windows launched by the start bat fallback
taskkill /F /FI "WINDOWTITLE eq Fortnite Overlay Server*" /T >nul 2>nul

echo.
echo [SUCCESS] The overlay has been completely stopped! Port 8888 is free.
timeout /t 3 /nobreak >nul