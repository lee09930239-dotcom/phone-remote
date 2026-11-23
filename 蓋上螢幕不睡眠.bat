@echo off
chcp 65001 >nul
echo === Lid close = do nothing, no auto sleep (plugged in AND battery) ===
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change monitor-timeout-ac 0
powercfg /change monitor-timeout-dc 0
powercfg /setactive SCHEME_CURRENT
echo.
echo --- Check (AC and DC should both be 0x00000000) ---
powercfg /q SCHEME_CURRENT SUB_BUTTONS LIDACTION | findstr /i "AC DC"
echo.
echo --- Company auto-lock policy (if a value shows, PC may lock itself) ---
reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v InactivityTimeoutSecs 2>nul
reg query "HKCU\Software\Policies\Microsoft\Windows\Control Panel\Desktop" /v ScreenSaveTimeOut 2>nul
reg query "HKLM\SOFTWARE\Policies\Microsoft\Windows\Control Panel\Desktop" /v ScreenSaveTimeOut 2>nul
echo.
echo Done. WARNING: the PC keeps running in your bag. Watch heat and battery.
pause
