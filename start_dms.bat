@echo off
REM DMS Server launcher for Windows.
REM Double-click this file to start the DMS.

cd /d "%~dp0"

echo.
echo ============================================================
echo   DMS Server launcher
echo ============================================================
echo.

REM Check for Python
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo.
    echo Install Python 3 from: https://www.python.org/downloads/
    echo During install, check "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

python --version

REM Check required packages
for %%P in (flask pypdf reportlab pillow) do (
    if "%%P"=="pillow" (
        python -c "import PIL" >nul 2>nul
    ) else (
        python -c "import %%P" >nul 2>nul
    )
    if errorlevel 1 (
        echo Installing missing package: %%P
        python -m pip install --user %%P
        if errorlevel 1 (
            echo.
            echo ERROR: Could not install %%P.
            echo Try running this manually:
            echo     python -m pip install %%P
            echo.
            pause
            exit /b 1
        )
    )
)

echo Python packages: OK
echo.

python dms_server.py %*
pause
