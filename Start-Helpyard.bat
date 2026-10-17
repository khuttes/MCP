@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0bridge.py" (
    echo Helpyard could not start: bridge.py was not found next to this launcher.
    pause
    exit /b 1
)

where py >nul 2>nul
if errorlevel 1 (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Helpyard needs Python 3.10 or newer. Install Python from https://www.python.org/downloads/ and enable the Python launcher.
        pause
        exit /b 1
    )
    python -c "import sys; raise SystemExit(sys.version_info < (3, 10))"
    if errorlevel 1 (
        echo Helpyard needs Python 3.10 or newer.
        pause
        exit /b 1
    )
    start "Helpyard Bridge" /min python "%~dp0bridge.py" --open-browser
    exit /b 0
)

py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
if not errorlevel 1 (
    start "Helpyard Bridge" /min py -3 "%~dp0bridge.py" --open-browser
    exit /b 0
)

where python >nul 2>nul
if errorlevel 1 (
    echo Helpyard needs Python 3.10 or newer. Install Python from https://www.python.org/downloads/ and enable the Python launcher.
    pause
    exit /b 1
)
python -c "import sys; raise SystemExit(sys.version_info < (3, 10))"
if errorlevel 1 (
    echo Helpyard needs Python 3.10 or newer.
    pause
    exit /b 1
)
start "Helpyard Bridge" /min python "%~dp0bridge.py" --open-browser
exit /b 0
