@echo off
REM ============================================================
REM  ClaudePet build script -> dist\ClaudePet.exe
REM  (skin folder is bundled inside the exe)
REM ============================================================

cd /d "%~dp0"

echo [1/3] Installing packages...
py -m pip install --upgrade pillow pyinstaller
if errorlevel 1 goto err

echo [2/3] Building exe (using temp dir to avoid long-path issues)...
set BUILDDIR=%TEMP%\claudepet_build
py -m PyInstaller --onefile --noconsole --name ClaudePet --icon "%~dp0claude_pet.ico" --add-data "%~dp0skin;skin" --workpath "%BUILDDIR%\work" --specpath "%BUILDDIR%" --distpath "%~dp0dist" "%~dp0claude_pet.py"
if errorlevel 1 goto err

echo [3/3] Done!
echo.
echo   Output: %~dp0dist\ClaudePet.exe
echo   Share this single file. (No Python needed on target PC)
echo   Note: target PC needs Claude Code login to show usage.
echo.
pause
exit /b 0

:err
echo.
echo BUILD FAILED. Check the error message above.
echo If 'py' is not found, edit this file: replace 'py' with 'python'.
pause
exit /b 1
