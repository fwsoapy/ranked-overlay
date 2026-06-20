@echo off
setlocal enabledelayedexpansion

set "API_KEY=5944cf9e101f8c722009a2dd790e705295555503d544144bfcd312af2eb0fa87"

:start
cls
echo ============================================
echo   Fortnite Account ID Lookup
echo ============================================
echo.
set /p "USERNAME=Enter Epic display name: "

if "%USERNAME%"=="" (
    echo No username entered.
    goto ask_again
)

echo.
echo Looking up: %USERNAME%
echo.

powershell -NoProfile -Command ^
  "$key = '%API_KEY%'; $name = [uri]::EscapeDataString('%USERNAME%'); $headers = @{'x-api-key'=$key}; $urls = @(\"https://prod.api-fortnite.com/api/v1/account/displayName/$name\", \"https://prod.api-fortnite.com/api/v1/profile/progress?displayName=$name\", \"https://prod.api-fortnite.com/api/v1/profile/stats?displayName=$name\"); $found = $false; foreach ($url in $urls) { try { $r = Invoke-RestMethod -Uri $url -Headers $headers -ErrorAction Stop; $json = $r | ConvertTo-Json -Depth 5; Write-Host \"URL: $url\"; Write-Host $json; $id = $r.accountId; if (-not $id) { $id = $r.account_id }; if (-not $id) { $id = $r.id }; if (-not $id -and $r.data) { $id = $r.data.accountId }; if (-not $id -and $r.data) { $id = $r.data.account_id }; if ($id) { Write-Host ''; Write-Host \"  Display Name : %USERNAME%\"; Write-Host \"  Account ID   : $id\"; $id | Set-Clipboard; Write-Host '  [Copied to clipboard]'; $found = $true; break } } catch { Write-Host \"Failed: $url\" } }; if (-not $found) { Write-Host 'Could not find account ID. See raw output above.' }"

echo.
:ask_again
set /p "AGAIN=Look up another? (y/n): "
if /i "%AGAIN%"=="y" goto start

echo.
echo Done.
pause
