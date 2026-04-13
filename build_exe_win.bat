@echo off
setlocal enabledelayedexpansion

REM Build a Windows .exe using PyInstaller.
REM Run this in CMD or PowerShell on Windows.
REM Single-file output (green portable exe):
REM   dist\dsl_stepper_speed_gui_win.exe

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"
set DIST_EXE=%SCRIPT_DIR%dist\dsl_stepper_speed_gui_win.exe
set RELEASE_EXE=%SCRIPT_DIR%release\dsl_stepper_speed_gui_win.exe

where py >nul 2>nul
if %errorlevel%==0 (
  set PY=py -3
  goto have_py
)

where python >nul 2>nul
if %errorlevel%==0 (
  set PY=python
  goto have_py
)

echo Python not found. Please install Python 3 from python.org and ensure it is added to PATH.
pause
exit /b 2

:have_py
echo Using: %PY%
echo.
echo Checking for running old exe...
taskkill /f /im dsl_stepper_speed_gui_win.exe >nul 2>nul

if exist "%DIST_EXE%" (
  del /f /q "%DIST_EXE%" >nul 2>nul
)
if exist "%RELEASE_EXE%" (
  del /f /q "%RELEASE_EXE%" >nul 2>nul
)

if exist "%DIST_EXE%" (
  echo Failed to remove old dist exe:
  echo %DIST_EXE%
  echo.
  echo Please close the running program or browser page opened by the old exe, then try again.
  pause
  exit /b 1
)

REM Install PyInstaller if missing
%PY% -m PyInstaller --version >nul 2>nul
if not %errorlevel%==0 (
  echo PyInstaller not found, installing...
  %PY% -m pip install --upgrade pip
  %PY% -m pip install pyinstaller
)

echo Building exe...
%PY% -m PyInstaller --clean --noconfirm dsl_stepper_speed_gui_win.spec
if not %errorlevel%==0 (
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo Build OK.
echo EXE path:
echo %DIST_EXE%
echo.

REM Optional: copy to a simple release folder for sharing.
if not exist "%SCRIPT_DIR%release" (
  mkdir "%SCRIPT_DIR%release" >nul 2>nul
)
copy /y "%DIST_EXE%" "%RELEASE_EXE%" >nul 2>nul
echo Copied to:
echo %RELEASE_EXE%
echo.
pause
exit /b 0
