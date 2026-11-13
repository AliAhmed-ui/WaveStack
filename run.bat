@echo off
setlocal EnableExtensions
title WaveStack Setup
cd /d "%~dp0"

set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "VENV_PY=%PROJECT_DIR%\venv\Scripts\python.exe"
set "VENV_PYW=%PROJECT_DIR%\venv\Scripts\pythonw.exe"
set "MARKER=%PROJECT_DIR%\.wavestack-setup-complete"
set "VLC_DLL=%ProgramFiles%\VideoLAN\VLC\libvlc.dll"
set "PY="

REM ---- Fast path: already set up, just launch -------------------------
if exist "%MARKER%" if exist "%VENV_PY%" goto launch

echo ============================================
echo   WaveStack - first-time setup for Windows
echo ============================================
echo.

REM ---- 1. Python 3 (with Tk) ------------------------------------------
call :find_python
if defined PY goto have_python

echo Python 3 was not found. Installing Python 3.12 with winget...
where winget >nul 2>&1
if errorlevel 1 (
    echo.
    echo winget is not available on this PC.
    echo Please install Python 3.12 from https://www.python.org/downloads/
    echo ^(tick "Add python.exe to PATH"^), then double-click this file again.
    goto fail
)
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :find_python
if not defined PY (
    echo.
    echo Python was installed but could not be located yet.
    echo Close this window and double-click run.bat once more.
    goto fail
)

:have_python
echo Using Python: %PY%

REM ---- 2. VLC (audio engine fallback and python-vlc backend) ----------
if exist "%VLC_DLL%" goto have_vlc
echo.
echo Installing VLC media player with winget...
where winget >nul 2>&1
if errorlevel 1 (
    echo winget is not available. Install 64-bit VLC from https://www.videolan.org/vlc/
    echo and double-click this file again.
    goto fail
)
winget install -e --id VideoLAN.VLC --silent --accept-package-agreements --accept-source-agreements
if not exist "%VLC_DLL%" (
    echo.
    echo VLC did not install. Install 64-bit VLC from https://www.videolan.org/vlc/
    echo and double-click this file again.
    goto fail
)
:have_vlc

REM ---- 3. Virtual environment + Python packages -----------------------
echo.
echo Creating WaveStack's Python environment...
if exist "%PROJECT_DIR%\venv" rmdir /s /q "%PROJECT_DIR%\venv"
"%PY%" -m venv "%PROJECT_DIR%\venv"
if errorlevel 1 goto fail
"%VENV_PY%" -m pip install --upgrade pip
echo Installing WaveStack's Python dependencies...
"%VENV_PY%" -m pip install -r "%PROJECT_DIR%\requirements.txt"
if errorlevel 1 goto fail

REM ---- 4. Desktop shortcut --------------------------------------------
echo.
echo Creating Desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\WaveStack.lnk'); $s.TargetPath='%PROJECT_DIR%\run.bat'; $s.WorkingDirectory='%PROJECT_DIR%'; $s.WindowStyle=7; $s.Description='WaveStack - 1990s MP3 player'; $s.IconLocation='%VLC_DLL:libvlc.dll=vlc.exe%'; $s.Save()" >nul 2>&1

echo setup-complete> "%MARKER%"

REM ---- 5. Native audio engine (optional) ------------------------------
REM Needs MSVC Build Tools; without them WaveStack uses VLC instead.
:launch
if exist "%PROJECT_DIR%\wavestack_audio*.pyd" goto run_app
where cl >nul 2>&1
if errorlevel 1 goto run_app
echo Building the native audio engine...
"%VENV_PY%" setup.py build_ext --inplace >nul 2>&1

:run_app
echo.
echo Starting WaveStack...
REM pythonw has no console window; start detaches so this window closes.
start "" "%VENV_PYW%" "%PROJECT_DIR%\wavestack.py" %*
endlocal
exit /b 0

REM ---------------------------------------------------------------------
:find_python
set "PY="
for %%C in ("%LocalAppData%\Programs\Python\Python312\python.exe" "%LocalAppData%\Programs\Python\Python313\python.exe" "%LocalAppData%\Programs\Python\Python311\python.exe" "%ProgramFiles%\Python312\python.exe" "%ProgramFiles%\Python313\python.exe") do (
    if not defined PY if exist %%C call :try_python %%C
)
if not defined PY for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PY call :try_python "%%P"
)
if not defined PY call :try_launcher
exit /b 0

:try_launcher
REM Last resort: ask the py launcher where Python 3 lives.
where py >nul 2>&1
if errorlevel 1 exit /b 0
set "PYCAND="
py -3 -c "import sys;print(sys.executable)" > "%TEMP%\wavestack_py.txt" 2>nul
if exist "%TEMP%\wavestack_py.txt" set /p PYCAND=< "%TEMP%\wavestack_py.txt"
del "%TEMP%\wavestack_py.txt" >nul 2>&1
if defined PYCAND call :try_python "%PYCAND%"
exit /b 0

:try_python
REM Accept only a real Python 3.9+ that has Tk (skips the Microsoft Store stub).
"%~1" -c "import sys,tkinter;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0

:fail
echo.
echo Setup did not finish. See the messages above.
pause
endlocal
exit /b 1
