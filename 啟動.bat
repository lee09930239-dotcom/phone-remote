@echo off
chcp 65001 >nul
cd /d "%~dp0"
taskkill /im cloudflared.exe /f >nul 2>nul
rem v1.13: no auto-update at start (safety). Update only from the phone menu button.
if not exist cloudflared.exe (
  echo Downloading cloudflared.exe ^(first time only, about 60MB^)...
  curl.exe -L -o cloudflared.exe https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
)
echo === PhoneRemote running. Close this window to stop. ===
where uv >nul 2>nul
if %errorlevel%==0 (
  uv run --python 3.12 --with aiohttp --with mss --with pillow phone_remote.py
) else (
  py -m pip install --user aiohttp mss pillow
  py phone_remote.py
)
taskkill /im cloudflared.exe /f >nul 2>nul
echo.
echo === Stopped. If there is an error above, screenshot this window for Claude ===
pause
