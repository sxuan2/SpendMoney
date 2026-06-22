@echo off
cd /d "%~dp0"

set "ACTIVATE_BAT="
if exist "D:\Programs\Anaconda\Scripts\activate.bat" set "ACTIVATE_BAT=D:\Programs\Anaconda\Scripts\activate.bat"
if not defined ACTIVATE_BAT if exist "%USERPROFILE%\Anaconda3\Scripts\activate.bat" set "ACTIVATE_BAT=%USERPROFILE%\Anaconda3\Scripts\activate.bat"
if not defined ACTIVATE_BAT if exist "%USERPROFILE%\miniconda3\Scripts\activate.bat" set "ACTIVATE_BAT=%USERPROFILE%\miniconda3\Scripts\activate.bat"
if not defined ACTIVATE_BAT if exist "C:\ProgramData\Anaconda3\Scripts\activate.bat" set "ACTIVATE_BAT=C:\ProgramData\Anaconda3\Scripts\activate.bat"

if not defined ACTIVATE_BAT (
    echo Could not find activate.bat.
    pause
    exit /b 1
)

call "%ACTIVATE_BAT%" pytest
python -m pip install --upgrade pip setuptools wheel
pip install --upgrade --force-reinstall -r requirements.txt
echo.
echo OCR environment setup finished in conda env: pytest.
echo You can now run run.bat