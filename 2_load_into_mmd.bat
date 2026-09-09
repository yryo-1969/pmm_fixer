@echo off
if "%~1"=="" (
    echo Drop a .pmm file onto this bat file.
    pause
    exit /b
)
cd /d "%~dp0"
if exist "pmm_fixer.exe" (
    pmm_fixer.exe load "%~1"
) else (
    python pmm_fixer.py load "%~1"
)
echo.
pause
