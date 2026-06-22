@echo off
echo [1] Starting run.bat...
cd /d "%~dp0"

echo [2] Locating Conda activation script...
set "ACTIVATE_BAT="
if exist "D:\Programs\Anaconda\Scripts\activate.bat" set "ACTIVATE_BAT=D:\Programs\Anaconda\Scripts\activate.bat"
if not defined ACTIVATE_BAT if exist "%USERPROFILE%\Anaconda3\Scripts\activate.bat" set "ACTIVATE_BAT=%USERPROFILE%\Anaconda3\Scripts\activate.bat"
if not defined ACTIVATE_BAT if exist "%USERPROFILE%\miniconda3\Scripts\activate.bat" set "ACTIVATE_BAT=%USERPROFILE%\miniconda3\Scripts\activate.bat"
if not defined ACTIVATE_BAT if exist "C:\ProgramData\Anaconda3\Scripts\activate.bat" set "ACTIVATE_BAT=C:\ProgramData\Anaconda3\Scripts\activate.bat"

if not defined ACTIVATE_BAT (
    echo [!] Could not find activate.bat in common locations.
    pause
    exit /b 1
)

echo [3] Activating environment 'pytest'...
call "%ACTIVATE_BAT%" pytest

echo [4] Checking Python environment...
python -c "import fastapi, uvicorn, paddle, paddleocr" >nul 2>&1
if errorlevel 1 (
    echo [!] Environment check failed. Calling setup_ocr.bat...
    call setup_ocr.bat
)

echo [5] Cleaning up potential port 8000 conflicts...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000" ^| findstr "LISTENING"') do (
    echo [!] Found background process on port 8000, killing PID %%a...
    taskkill /F /PID %%a >nul 2>&1
)

echo [6] Starting SpendMoney Server...
start "SpendMoney Server" cmd /k "cd /d "%~dp0" && call "%ACTIVATE_BAT%" pytest && uvicorn server:app --host 0.0.0.0 --port 8000"

echo [7] Waiting for server to initialize...
ping -n 4 127.0.0.1 >nul

echo [8] Opening browser...
start http://127.0.0.1:8000

echo [9] Script executed completely.
@REM pause