@echo off
if "%~1"=="" (
    echo Drop a .pmm file onto this bat file.
    pause
    exit /b
)
cd /d "%~dp0"
if exist "pmm_fixer.exe" (
    pmm_fixer.exe scan "%~1"
) else (
    python pmm_fixer.py scan "%~1"
)
echo.
echo Done. Check the Excel report next to this bat file.
pause
