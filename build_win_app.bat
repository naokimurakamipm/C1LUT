@echo off
REM C1LUT Windows build driver. Same contract as before:
REM   build_win_app.bat [--no-pause]  ->  dist\C1LUT\C1LUT.exe  (exit 0/1)
setlocal
cd /d "%~dp0"
set "RC=1"
set "PAUSE_AT_END=1"
if /i "%~1"=="--no-pause" set "PAUSE_AT_END=0"

if not exist "native\lcms2.dll" (
    echo [error] native\lcms2.dll is missing. Fetch it first:
    echo         .venv\Scripts\python tools\fetch_lcms.py
    goto :finish
)

REM Try project venv first, then the launcher, then PATH python.
call :try_build ".venv\Scripts\python.exe" && goto :finish
if errorlevel 2 goto :finish
call :try_build "py -3.12" && goto :finish
if errorlevel 2 goto :finish
call :try_build "python" && goto :finish
if errorlevel 2 goto :finish

echo [error] no usable Python with PyInstaller was found.
echo         py -3.12 -m pip install pyinstaller==6.17.0

:finish
if "%PAUSE_AT_END%"=="1" pause
exit /b %RC%

REM ---- try_build <python command>: 0 = built, 1 = interpreter absent, 2 = build failed
:try_build
%~1 --version >nul 2>nul || exit /b 1
echo Building C1LUT with: %~1
%~1 -m PyInstaller --noconfirm --clean C1LUT.spec
if errorlevel 1 (
    echo [error] PyInstaller failed - see its output above.
    exit /b 2
)
echo.
echo OK: dist\C1LUT\C1LUT.exe  ^(ship the entire dist\C1LUT folder^)
set "RC=0"
exit /b 0
