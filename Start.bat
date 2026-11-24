@echo off
setlocal
cd /d "%~dp0"
set "ENV_DIR=%LOCALAPPDATA%\AdarshSupplyChainControl\.venv"

if not exist "%ENV_DIR%\Scripts\python.exe" (
    echo Run Setup.bat once before starting the dashboard.
    pause
    exit /b 1
)

"%ENV_DIR%\Scripts\python.exe" -m streamlit run app.py
if errorlevel 1 (
    echo The dashboard stopped. Run Setup.bat to repair the environment.
    pause
    exit /b 1
)
