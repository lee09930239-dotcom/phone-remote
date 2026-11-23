# phone-remote

手機用瀏覽器遙控 Windows 筆電（看畫面、點擊、打字、音量、媒體鍵、PPT 換頁）。

- 筆電端：`phone_remote.py`（aiohttp，只聽 127.0.0.1:8777），外網用 cloudflared quick tunnel，新網址用 Outlook 寄給自己
- 啟動：雙擊 `啟動.bat`（每次啟動會自動從這個 repo 下載最新的 `phone_remote.py`）
- 蓋上螢幕不睡眠：`蓋上螢幕不睡眠.bat`／還原：`還原電源設定.bat`
- 詳細步驟：`手機連線步驟.txt`

PIN 等設定存在本機 `phone_remote_config.json`，不會上傳。
