@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

if not exist "hh_selenium_config.json" (
    if exist "hh_selenium_config.example.json" (
        copy "hh_selenium_config.example.json" "hh_selenium_config.json" >nul
    )
)

python hh_selenium.py --api-cache --limit 200 %*
set EXIT_CODE=%ERRORLEVEL%
echo.
pause
exit /b %EXIT_CODE%
