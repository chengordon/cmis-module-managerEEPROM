@echo off
title CMIS Module Manager

cd /d "%~dp0"

echo Starting CMIS Module Manager...
echo Open browser: http://127.0.0.1:5000
echo ========================================
set PYTHON=.\python32_env\python.exe
%PYTHON% app.py

echo.
echo Server stopped. Press any key to close.
pause >nul