@echo off
if "%~1"=="" (
    echo Drop a .pmm file onto this bat file.
    pause
    exit /b
)
cd /d "%~dp0"
python pmm_fixer.py scan "%~1"
echo.
echo Done. Check the Excel report next to pmm_fixer.py.
pause
