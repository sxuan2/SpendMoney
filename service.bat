@echo off
cd /d "%~dp0"

set "ACTIVATE_BAT="
if exist "D:\Programs\Anaconda\Scripts\activate.bat" set "ACTIVATE_BAT=D:\Programs\Anaconda\Scripts\activate.bat"
if not defined ACTIVATE_BAT if exist "%USERPROFILE%\Anaconda3\Scripts\activate.bat" set "ACTIVATE_BAT=%USERPROFILE%\Anaconda3\Scripts\activate.bat"
if not defined ACTIVATE_BAT if exist "%USERPROFILE%\miniconda3\Scripts\activate.bat" set "ACTIVATE_BAT=%USERPROFILE%\miniconda3\Scripts\activate.bat"
if not defined ACTIVATE_BAT if exist "C:\ProgramData\Anaconda3\Scripts\activate.bat" set "ACTIVATE_BAT=C:\ProgramData\Anaconda3\Scripts\activate.bat"

if not defined ACTIVATE_BAT (
    echo Could not find activate.bat
    exit /b 1
)

call "%ACTIVATE_BAT%" pytest

python -c "import fastapi, uvicorn, paddle, paddleocr"
if errorlevel 1 (
    echo Environment check failed
    exit /b 1
)

for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a
)

uvicorn server:app --host 0.0.0.0 --port 8000