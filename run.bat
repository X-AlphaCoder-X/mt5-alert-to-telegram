@echo off
setlocal
REM ===================================================================
REM  MT5 Alert to Telegram - launcher
REM  Double-click this file. It runs mt5_alert_to_telegram.py from this folder.
REM ===================================================================

REM --- 1. Your Python --------------------------------------------------
REM  PUT YOUR PYTHON PATH HERE: the full path to python.exe, without quotes.
REM  Example for Miniconda:   set "PYTHON=C:\ProgramData\miniconda3\python.exe"
REM  Example for an env:      set "PYTHON=C:\ProgramData\miniconda3\envs\mt5\python.exe"
REM  Leave it empty to use the Python launcher (py) or the python on your PATH.
set "PYTHON="

REM --- 2. Miniconda or Anaconda only (optional) -------------------------
REM  PUT YOUR CONDA FOLDER HERE to activate an environment first, for example
REM  set "CONDA=C:\ProgramData\miniconda3"  and  set "CONDA_ENV=base".
REM  Leave CONDA empty if you don't use conda.
set "CONDA="
set "CONDA_ENV=base"

REM --- nothing to change below this line --------------------------------
cd /d "%~dp0"

if not "%CONDA%"=="" call "%CONDA%\Scripts\activate.bat" %CONDA_ENV%

set "PYARGS="
if not "%PYTHON%"=="" goto have_python
if not "%CONDA%"=="" (
    set "PYTHON=python"
    goto have_python
)
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    set "PYTHON=py"
    set "PYARGS=-3"
) else (
    set "PYTHON=python"
)
:have_python

REM Administrator is only needed when MT5 itself runs as Administrator.
net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo WARNING: this window is not running as Administrator.
    echo If MT5 runs as Administrator, the script can't read its windows.
    echo In that case close this window, right-click run.bat and choose Run as administrator.
    echo.
)

"%PYTHON%" %PYARGS% -c "import pywinauto, psutil, requests, comtypes" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Some Python packages are missing, or Python was not found.
    echo Install the packages with:
    echo     "%PYTHON%" %PYARGS% -m pip install -r requirements.txt
    echo Or set PYTHON at the top of this file to the python.exe you use.
    echo.
    pause
    exit /b 1
)

"%PYTHON%" %PYARGS% "mt5_alert_to_telegram.py" %*
pause
