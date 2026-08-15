@echo off
title Fantasy Football Terminal 2026
color 0A

echo.
echo  =============================================================
echo    FANTASY DRAFT TERMINAL v1.0
echo    ESPN Fantasy Football // Live Draft Assistant // 2026
echo  =============================================================
echo.

:: ── Check Python is installed ──────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    color 0C
    echo  [ERROR] Python not found on this computer.
    echo.
    echo  Please install Python 3.11 or newer from:
    echo  https://www.python.org/downloads/
    echo.
    echo  IMPORTANT: Check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

echo  [OK] Python found.

:: ── Create virtual environment if it doesn't exist ─────────────────────────
if not exist "env\" (
    echo  [..] Creating virtual environment...
    python -m venv env
    if %errorlevel% neq 0 (
        color 0C
        echo  [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo  [OK] Virtual environment created.
) else (
    echo  [OK] Virtual environment already exists.
)

:: ── Install / update dependencies ──────────────────────────────────────────
echo  [..] Installing dependencies (first run may take 1-2 minutes)...
env\Scripts\pip install -r requirements.txt --quiet --disable-pip-version-check
if %errorlevel% neq 0 (
    color 0C
    echo  [ERROR] Failed to install dependencies.
    echo  Check your internet connection and try again.
    pause
    exit /b 1
)
echo  [OK] Dependencies ready.

:: ── Check port 8888 isn't already in use ───────────────────────────────────
netstat -ano | findstr ":8888 " >nul 2>&1
if %errorlevel% equ 0 (
    echo  [WARN] Port 8888 already in use. Stopping existing process...
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8888 "') do (
        taskkill /PID %%a /F >nul 2>&1
    )
    timeout /t 2 /nobreak >nul
)

:: ── Start the server ───────────────────────────────────────────────────────
echo  [..] Starting Fantasy Draft server on http://localhost:8888 ...
start "" /B env\Scripts\python server.py

:: ── Wait for server to be ready ────────────────────────────────────────────
echo  [..] Waiting for server to start...
set /a tries=0
:waitloop
timeout /t 1 /nobreak >nul
curl -s http://localhost:8888/api/state >nul 2>&1
if %errorlevel% equ 0 goto ready
set /a tries+=1
if %tries% lss 20 goto waitloop

color 0C
echo  [ERROR] Server failed to start after 20 seconds.
echo  Check that LEAGUE_ID, espn_s2 and SWID are filled in at the top of server.py.
pause
exit /b 1

:ready
echo  [OK] Server is running.
echo.
echo  =============================================================
echo    Opening browser...
echo    URL: http://localhost:8888
echo.
echo    - Select your strategy
echo    - Enter your draft position when known
echo    - Click ACTIVATE LIVE when draft begins
echo.
echo    Press Ctrl+C in this window to stop the server.
echo  =============================================================
echo.

:: ── Open browser ───────────────────────────────────────────────────────────
start "" http://localhost:8888

:: ── Keep window open so server keeps running ───────────────────────────────
:: (server already running in background from the start /B above — do NOT
::  launch it a second time or it errors with "port already in use")
echo  Server running. Close this window to stop it.
echo.
pause >nul
