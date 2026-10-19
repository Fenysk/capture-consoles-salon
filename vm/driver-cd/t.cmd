@echo off
rem Test GL310 : options du peripherique puis capture 60 s (demarre l encodeur, comme une appli).
set "F=%~dp0ffmpeg\ffmpeg.exe"
set "D=AVerMedia C835 Capture"
echo === options video ===
"%F%" -hide_banner -list_options true -f dshow -i video="%D%" 2>&1
echo === capture 60 s ===
"%F%" -hide_banner -y -rtbufsize 256M -f dshow -i video="%D%" -t 60 -c copy C:\Users\Public\test.mkv 2>&1
echo === fin ===
