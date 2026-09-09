@echo off
cd /d "%~dp0"
if exist "app\pmm_fixer.exe" (
    "app\pmm_fixer.exe" scan %*
) else if exist "pmm_fixer.exe" (
    pmm_fixer.exe scan %*
) else (
    python pmm_fixer.py scan %*
)
echo.
echo Done. Check the Excel report next to this bat file.
pause
