@echo off
setlocal enabledelayedexpansion

set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="

set "NO_PROXY=*"
set "no_proxy=*"

set "PROJECT_DIR=D:\project\Firefight"
set "ENV_NAME=fire_fighting"
set "APP_IMPORT=main:app"
set "HOST=0.0.0.0"
set "PORT=8005"
set "UVICORN_ARGS=--reload --host %HOST% --port %PORT%"

title Firefight API (%ENV_NAME%) %HOST%:%PORT%

cd /d "%PROJECT_DIR%" || (
  echo [ERROR] Cannot cd to: %PROJECT_DIR%
  pause
  exit /b 1
)

set "CONDA_BAT="
for /f "delims=" %%i in ('where conda.bat 2^>nul') do ( set "CONDA_BAT=%%i" & goto :got )
if exist "%USERPROFILE%\miniconda3\condabin\conda.bat" set "CONDA_BAT=%USERPROFILE%\miniconda3\condabin\conda.bat"
if not defined CONDA_BAT if exist "%USERPROFILE%\anaconda3\condabin\conda.bat" set "CONDA_BAT=%USERPROFILE%\anaconda3\condabin\conda.bat"
if not defined CONDA_BAT if exist "C:\ProgramData\miniconda3\condabin\conda.bat" set "CONDA_BAT=C:\ProgramData\miniconda3\condabin\conda.bat"
if not defined CONDA_BAT if exist "C:\ProgramData\anaconda3\condabin\conda.bat" set "CONDA_BAT=C:\ProgramData\anaconda3\condabin\conda.bat"
:got

if not defined CONDA_BAT (
  echo [ERROR] conda.bat not found. Try: where conda.bat
  pause
  exit /b 1
)

call "%CONDA_BAT%" activate "%ENV_NAME%"
if errorlevel 1 (
  echo [ERROR] conda activate failed: %ENV_NAME%
  pause
  exit /b 1
)

where uvicorn >nul 2>&1
if errorlevel 1 (
  echo [ERROR] uvicorn not found in env. Run: pip install uvicorn[standard]
  pause
  exit /b 1
)

echo [INFO] Starting: http://%HOST%:%PORT%
uvicorn %APP_IMPORT% %UVICORN_ARGS%

echo.
echo [INFO] Uvicorn exited with code %ERRORLEVEL%
pause
