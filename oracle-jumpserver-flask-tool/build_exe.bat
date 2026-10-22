@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install "pyinstaller>=6,<7" || exit /b 1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean oracle_jump_tool.spec || exit /b 1
copy /y ".env.example" "dist\.env.example" >nul
echo.
echo Built dist\oracle-jump-tool.exe. Put a .env next to it (copy .env.example) and run it.
endlocal
