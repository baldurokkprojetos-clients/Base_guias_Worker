@echo off
echo Stopping Base Guias Unimed System...

echo Killing Worker Server...
taskkill /FI "WINDOWTITLE eq Worker Server 8010*" /T /F 2>nul

echo Killing Dispatcher...
taskkill /FI "WINDOWTITLE eq Dispatcher*" /T /F 2>nul


echo Killing Chrome Driver...
taskkill /F /IM chromedriver.exe /T 2>nul

echo.
echo All servers and workers have been stopped.
echo You can now close the remaining empty CMD windows if desired.
pause
