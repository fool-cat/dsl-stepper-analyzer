@echo off
setlocal

set SCRIPT_DIR=%~dp0

REM Allow drag-and-drop of .dsl files onto this .bat.
REM If no args are given, the Python GUI will pop up a file picker.

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%SCRIPT_DIR%dsl_stepper_speed_gui_win.py" %*
  exit /b %errorlevel%
)

where python >nul 2>nul
if %errorlevel%==0 (
  python "%SCRIPT_DIR%dsl_stepper_speed_gui_win.py" %*
  exit /b %errorlevel%
)

echo Python not found. Please install Python 3 from python.org and ensure it is added to PATH.
pause
exit /b 2

