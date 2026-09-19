@echo off
setlocal enabledelayedexpansion
rem Build the C-One LUT Windows installer (requires the dist build first).
rem Usage: build_installer.bat [--no-pause]
set NO_PAUSE=%1
cd /d "%~dp0"

rem 1) Make sure the app build exists.
if not exist "dist\COneLUT\COneLUT.exe" (
    echo dist build missing - building it first.
    call build_win_app.bat --no-pause
    if not exist "dist\COneLUT\COneLUT.exe" (
        echo ERROR: app build failed; installer aborted.
        goto :fail
    )
)

rem 2) Locate the Inno Setup compiler (machine-wide or per-user installs).
set ISCC=
for %%P in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
) do (
    if not defined ISCC if exist %%P set ISCC=%%~P
)
if not defined ISCC (
    echo ERROR: Inno Setup 6 not found. Install it with:
    echo    winget install JRSoftware.InnoSetup
    goto :fail
)

echo Compiling installer with: %ISCC%
"%ISCC%" "installer\COneLUT.iss"
if errorlevel 1 goto :fail

for %%F in ("installer\Output\COneLUT-Setup-*.exe") do set SETUP=%%~fF
echo.
echo OK: %SETUP%
echo The installer is a local build artifact - it is gitignored and never pushed.
if not "%NO_PAUSE%"=="--no-pause" pause
exit /b 0

:fail
if not "%NO_PAUSE%"=="--no-pause" pause
exit /b 1
