@echo off
rem Installe le pilote GL310 depuis ce CD et journalise le resultat.
set "LOG=C:\Users\Public\gl310.log"
echo === install %DATE% %TIME% === >> "%LOG%"
pnputil /add-driver "%~dp0GL310\AVer835_x64.inf" /install >> "%LOG%" 2>&1
echo code retour %ERRORLEVEL% >> "%LOG%"
pnputil /enum-devices /connected /class Media >> "%LOG%" 2>&1
type "%LOG%"
