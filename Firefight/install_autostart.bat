@echo off
setlocal

set "TARGET=%~dp0start_firefight.bat"
set "LNKNAME=Firefight_API.lnk"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$target='%TARGET%';" ^
  "$startup=[Environment]::GetFolderPath('Startup');" ^
  "$lnk=Join-Path $startup '%LNKNAME%';" ^
  "$w=New-Object -ComObject WScript.Shell;" ^
  "$s=$w.CreateShortcut($lnk);" ^
  "$s.TargetPath=$target;" ^
  "$s.WorkingDirectory=(Split-Path $target);" ^
  "$s.WindowStyle=7;" ^
  "$s.Save();"

if errorlevel 1 (
  echo [ERROR] Failed to create Startup shortcut.
  pause
  exit /b 1
)

echo [OK] Added to Startup: %LNKNAME%
echo Remove: Win+R  then type: shell:startup  then delete %LNKNAME%
pause
