@echo off
setlocal

REM Check if pip is installed
python -m pip --version >nul 2>&1
if errorlevel 1 (
    echo Pip not found. Attempting to install...
    python -m ensurepip --upgrade
    if errorlevel 1 (
        echo Failed to install pip. Please install it manually.
        pause
        exit /b 1
    )
) else (
    echo Pip is installed.
)

REM Check if pyinstaller is installed
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    pip install pyinstaller
    if errorlevel 1 (
        echo Failed to install PyInstaller. Please install it manually.
        pause
        exit /b 1
    )
) else (
    echo PyInstaller is already installed.
)

REM Run pyinstaller
echo Building executable...
pyinstaller --onefile --icon=icon.ico storemio.py

echo Build complete. Executable should be in the "dist" folder.
pause
