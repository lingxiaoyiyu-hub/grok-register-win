@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Grok Register Win
set "ROOT=%CD%\"
set "LOG=%CD%\data\logs\start.log"
if not exist "%CD%\data\logs" mkdir "%CD%\data\logs"
>>"%LOG%" echo ===== %date% %time% start =====

echo.
echo  [Grok Register Win]
echo  Folder: %CD%
echo.

REM ---- pick Python ----
set "PY="
where py >nul 2>nul
if not errorlevel 1 (
  for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%I"
)
if not defined PY goto :find_python
goto :have_python

:find_python
where python >nul 2>nul
if errorlevel 1 goto :no_python
for /f "delims=" %%I in ('where python') do call :maybe_set_py "%%I"
if not defined PY goto :no_python
goto :have_python

:maybe_set_py
echo %~1 | find /i "\WindowsApps\" >nul
if not errorlevel 1 goto :eof
if defined PY goto :eof
set "PY=%~1"
goto :eof

:no_python
echo [ERROR] Python not found. Install Python 3.10+ and check Add to PATH.
>>"%LOG%" echo [ERROR] Python not found.
goto :fail

:have_python
echo [OK] Python: %PY%
>>"%LOG%" echo [OK] Python: %PY%
"%PY%" --version
"%PY%" --version >>"%LOG%" 2>&1

REM ---- venv ----
if exist "%CD%\.venv\Scripts\python.exe" goto :venv_ok
echo [..] Creating venv .venv ...
"%PY%" -m venv "%CD%\.venv"
if errorlevel 1 (
  echo [ERROR] venv create failed
  >>"%LOG%" echo [ERROR] venv create failed
  goto :fail
)

:venv_ok
set "VPY=%CD%\.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo [ERROR] venv python missing
  goto :fail
)
echo [OK] Venv: %VPY%

REM ---- deps ----
"%VPY%" -c "import flask,DrissionPage,curl_cffi" >nul 2>nul
if not errorlevel 1 goto :deps_ok
echo [..] Installing dependencies, first run may take minutes...
"%VPY%" -m pip install -U pip >>"%LOG%" 2>&1
"%VPY%" -m pip install -r "%CD%\requirements.txt" >>"%LOG%" 2>&1
if errorlevel 1 (
  echo [..] Retry with Tsinghua mirror...
  "%VPY%" -m pip install -r "%CD%\requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple >>"%LOG%" 2>&1
)
"%VPY%" -c "import flask,DrissionPage,curl_cffi" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Dependency install failed. See data\logs\start.log
  type "%LOG%"
  goto :fail
)
echo [OK] Dependencies installed
goto :after_deps

:deps_ok
echo [OK] Dependencies already present

:after_deps
echo [..] Applying Playwright patch (Firefox pageError crash fix)...
"%VPY%" "%CD%\lib\patch_playwright.py" >>"%LOG%" 2>&1
if exist "%CD%\config.json" goto :cfg_ok
copy /Y "%CD%\config.example.json" "%CD%\config.json" >nul
echo [OK] Created config.json

:cfg_ok
echo.
echo  Before register:
echo    1. Start Clash - HTTP or mixed port 7890/7897
echo    2. Pick a working node in Clash
echo    3. Chrome/Edge ^(chromium^) or Camoufox headless via panel dropdown
echo.
echo  Panel:    http://127.0.0.1:8787
echo  Log:      data\logs\start.log
echo.

"%VPY%" "%CD%\launcher.py"
set "ERR=%ERRORLEVEL%"
echo.
echo  Exit code: %ERR%
>>"%LOG%" echo Exit code: %ERR%
if not "%ERR%"=="0" goto :fail
echo.
pause
exit /b 0

:fail
echo.
echo  ========== FAILED ==========
echo  Keep this window open. Check:
echo    - Python 3.10+ installed
echo    - data\logs\start.log
echo  ============================
echo.
pause
exit /b 1
