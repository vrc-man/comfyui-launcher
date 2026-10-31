@echo off
rem 创建 venv 并安装 PyInstaller
chcp 65001 >nul
cd /d "%~dp0"
set "BASE_PY=I:\cc\Py64-311\python-3.11.8.amd64\python.exe"
if not exist "%BASE_PY%" (
    echo [ERROR] Base python not found: %BASE_PY%
    pause
    exit /b 1
)
"%BASE_PY%" -m venv --prompt="comfy-launcher" "%~dp0comfyui-launcher-venv"
if errorlevel 1 (
    echo [ERROR] venv create failed
    pause
    exit /b 1
)
"%~dp0comfyui-launcher-venv\Scripts\pip.exe" install pyinstaller
if errorlevel 1 (
    echo [ERROR] pyinstaller install failed
    pause
    exit /b 1
)
rem core.py 运行期依赖 FastAPI + uvicorn（缺了会在启动时 SystemExit）
"%~dp0comfyui-launcher-venv\Scripts\pip.exe" install fastapi "uvicorn[standard]"
if errorlevel 1 (
    echo [ERROR] fastapi/uvicorn install failed
    pause
    exit /b 1
)
echo [OK] venv + pyinstaller + fastapi/uvicorn ready
pause
