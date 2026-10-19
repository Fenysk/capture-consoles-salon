@echo off
rem Liste les peripheriques DirectShow (noms video/audio du GL310).
"%~dp0ffmpeg\ffmpeg.exe" -hide_banner -list_devices true -f dshow -i dummy > C:\Users\Public\devices.txt 2>&1
type C:\Users\Public\devices.txt
