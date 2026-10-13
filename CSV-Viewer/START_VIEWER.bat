@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 shaker_csv_viewer.py
goto finished
:use_python
python shaker_csv_viewer.py
:finished
if errorlevel 1 (
    echo.
    echo Viewer could not start. First run INSTALL_REQUIREMENTS.bat.
    echo Python 3.10 or newer with Tkinter is required.
    pause
)
