@echo off
rem 源码模式启动（需已创建 venv: comfyui-launcher-venv）
chcp 65001 >nul
cd /d "%~dp0"
if not exist "comfyui-launcher-venv\Scripts\pythonw.exe" (
    echo [ERROR] venv not found, run create_venv.bat first
    pause
    exit /b 1
)
start "" "comfyui-launcher-venv\Scripts\pythonw.exe" "%~dp0comfy_launcher.py"
