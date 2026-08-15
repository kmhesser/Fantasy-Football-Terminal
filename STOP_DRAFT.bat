@echo off
title Fantasy Draft - Stopping Server
echo Stopping Fantasy Draft server...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8888 "') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo Done. Server stopped.
timeout /t 2 /nobreak >nul
