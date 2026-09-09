@echo off
if "%~1"=="" (
    echo Drop a .pmm file onto this bat file.
    pause
    exit /b
)
cd /d "%~dp0"
python pmm_fixer.py load "%~1"
echo.
pause
