@echo off
setlocal

set "ROOT=%~dp0"
set "PYEXE=%LocalAppData%\Programs\Python\Python312\python.exe"
set "PYW=%LocalAppData%\Programs\Python\Python312\pythonw.exe"

if not exist "%PYEXE%" (
    echo Python 3.12 was not found at:
    echo %PYEXE%
    echo.
    echo Install Python 3.12 or edit START_GUI.bat to point to your Python installation.
    pause
    exit /b 1
)

if not exist "%PYW%" set "PYW=%PYEXE%"

"%PYEXE%" -c "import PySide6, pyqtgraph, pyvisa, serial" >nul 2>&1
if errorlevel 1 (
    echo Installing missing GUI dependencies into Python 3.12...
    "%PYEXE%" -m pip install -r "%ROOT%requirements.txt"
    if errorlevel 1 (
        echo.
        echo Dependency installation failed.
        pause
        exit /b 1
    )
)

start "" "%PYW%" "%ROOT%GUI\shaker_control_gui_CSU15_45N.py"
endlocal
