@echo off
rem 打包为单文件 GUI exe -> dist\comfy_launcher.exe
chcp 65001 >nul
cd /d "%~dp0"
if not exist "comfyui-launcher-venv\Scripts\pyinstaller.exe" (
    echo [ERROR] pyinstaller not found, run create_venv.bat first
    pause
    exit /b 1
)
"comfyui-launcher-venv\Scripts\pyinstaller.exe" --onefile --windowed --name comfy_launcher --clean --noconfirm comfy_launcher.py
if errorlevel 1 (
    echo [ERROR] build failed
    pause
    exit /b 1
)
echo [OK] Built: dist\comfy_launcher.exe
pause
