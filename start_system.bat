@echo off
echo Starting Base Guias Unimed System...
echo Note: Please allow access in Firewall if prompted.
echo (Recommended: use gui.py / BaseGuiasManager.exe instead of this legacy script)

echo Starting Worker Server...
start "Worker Server 8010" cmd /k "set PORT=8010 && python Worker/server.py"
timeout /t 1

echo Starting Dispatcher...
start "Dispatcher" cmd /k "set API_SERVER_URLS=http://127.0.0.1:8010 && python Worker/dispatcher.py"
timeout /t 1


echo.
echo ========================================================
echo System Started!
echo ========================================================
echo.
pause
