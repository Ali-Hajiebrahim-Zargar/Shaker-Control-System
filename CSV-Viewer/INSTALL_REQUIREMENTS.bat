@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -m pip install -r requirements.txt
goto finished
:use_python
python -m pip install -r requirements.txt
:finished
if errorlevel 1 (
    echo Installation failed. Check that Python 3.10 or newer is installed and internet is available.
) else (
    echo Installation complete. Double-click START_VIEWER.bat.
)
pause
