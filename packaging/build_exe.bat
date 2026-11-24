@echo off
title Build CMIS Module Manager EXE
set PYTHON=.\python32_env\python.exe
REM Run from the repository root so the spec's paths resolve correctly.
cd /d "%~dp0.."

echo Building CMIS_Module_Manager.exe ...
%PYTHON% -m PyInstaller "packaging\CMIS.spec" --clean --noconfirm ^
  --distpath "CMIS2Customer" --workpath "build"

if errorlevel 1 (
    echo BUILD FAILED.
    pause
    exit /b 1
)

rmdir /s /q "build" 2>nul

for %%D in (SLABHIDtoSMBus.dll SLABHIDDevice.dll) do (
  if exist "%%D" copy /y "%%D" "CMIS2Customer\%%D" >nul
)

echo.
echo Done: %~dp0..\CMIS2Customer\CMIS_Module_Manager.exe
pause
