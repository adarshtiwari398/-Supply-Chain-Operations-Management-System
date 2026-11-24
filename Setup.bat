@echo off
setlocal
cd /d "%~dp0"
set "ENV_DIR=%LOCALAPPDATA%\AdarshSupplyChainControl\.venv"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.10 or later, then run Setup.bat again.
    pause
    exit /b 1
)

if not exist "%ENV_DIR%\Scripts\python.exe" (
    python -m venv "%ENV_DIR%"
    if errorlevel 1 (
        echo Could not create the project environment.
        pause
        exit /b 1
    )
)

"%ENV_DIR%\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency setup failed. Check your internet connection and try again.
    pause
    exit /b 1
)

echo.
echo Setup is complete. Double-click Start.bat to open the dashboard.
pause
