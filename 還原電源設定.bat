@echo off
chcp 65001 >nul
echo === Restore: lid close = sleep; sleep 30/15 min; screen off 10/5 min (plugged/battery) ===
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 1
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 1
powercfg /change standby-timeout-ac 30
powercfg /change standby-timeout-dc 15
powercfg /change monitor-timeout-ac 10
powercfg /change monitor-timeout-dc 5
powercfg /setactive SCHEME_CURRENT
echo Done.
pause
