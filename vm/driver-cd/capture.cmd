@echo off
rem Capture 60 s depuis le GL310 : capture.cmd "nom video" "nom audio"
set "V=%~1"
set "A=%~2"
if "%V%"=="" set "V=AVerMedia GL310 USB HD Capture Device"
if "%A%"=="" ( "%~dp0ffmpeg\ffmpeg.exe" -hide_banner -y -f dshow -i video="%V%" -t 60 -c copy C:\Users\Public\test.mkv > C:\Users\Public\capture.txt 2>&1 ) else ( "%~dp0ffmpeg\ffmpeg.exe" -hide_banner -y -f dshow -i video="%V%":audio="%A%" -t 60 -c copy C:\Users\Public\test.mkv > C:\Users\Public\capture.txt 2>&1 )
type C:\Users\Public\capture.txt
