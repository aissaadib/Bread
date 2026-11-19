@echo off
set "PROJECT_ROOT=%~dp0"
set "SOURCE_LOCALAPPDATA=%LOCALAPPDATA%"
if exist "%PROJECT_ROOT%.venv\Scripts\python.exe" (
  set "LUNA_PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"
) else (
  if exist "%SOURCE_LOCALAPPDATA%\Python\bin\python.exe" set "LUNA_PYTHON=%SOURCE_LOCALAPPDATA%\Python\bin\python.exe"
  for /d %%P in ("%SOURCE_LOCALAPPDATA%\Programs\Python\Python*") do if exist "%%~fP\python.exe" set "LUNA_PYTHON=%%~fP\python.exe"
)
set "LUNA_DATA_DIR=%PROJECT_ROOT%.runtime\data"
set "TORCH_HOME=%PROJECT_ROOT%.runtime\torch"
set "APPDATA=%PROJECT_ROOT%.runtime\roaming"
set "LOCALAPPDATA=%PROJECT_ROOT%.runtime\local"
set "TEMP=%PROJECT_ROOT%.runtime\temp"
set "TMP=%PROJECT_ROOT%.runtime\temp"
if exist "%PROJECT_ROOT%.build-clips\release\luna.exe" (
  set "LUNA_EXE=%PROJECT_ROOT%.build-clips\release\luna.exe"
) else if exist "%PROJECT_ROOT%.build-final\release\luna.exe" (
  set "LUNA_EXE=%PROJECT_ROOT%.build-final\release\luna.exe"
) else if exist "%PROJECT_ROOT%.build-next\release\luna.exe" (
  set "LUNA_EXE=%PROJECT_ROOT%.build-next\release\luna.exe"
) else (
  set "LUNA_EXE=%PROJECT_ROOT%src-tauri\target\release\luna.exe"
)
start "Luna" "%LUNA_EXE%"
