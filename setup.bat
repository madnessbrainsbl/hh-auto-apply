@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

echo Installing Python dependencies...
python -m pip install --upgrade pip
if errorlevel 1 goto error

python -m pip install -r requirements.txt
if errorlevel 1 goto error

echo.
echo Setup complete.
pause
exit /b 0

:error
echo.
echo Setup failed.
pause
exit /b 1
