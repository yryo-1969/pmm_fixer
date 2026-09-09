@echo off
cd /d "%~dp0"
if exist "app\pmm_fixer.exe" (
    "app\pmm_fixer.exe" load %*
) else if exist "pmm_fixer.exe" (
    pmm_fixer.exe load %*
) else (
    python pmm_fixer.py load %*
)
echo.
pause
