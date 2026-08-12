@echo off
rem 打包为单文件 GUI exe -> dist\comfy_launcher.exe
rem 打包后自动生成 version.json（core.py 哈希 + 版本号），并复制 core.py 到 dist
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

rem 生成 version.json（供启动器检测更新）
"comfyui-launcher-venv\Scripts\python.exe" make_version.py
if errorlevel 1 (
    echo [WARN] make_version.py failed
)

rem 复制 core.py 到 dist（分发目录 = exe + core.py + version.json）
copy /Y core.py dist\core.py >nul
if not exist "dist\version.json" (
    copy /Y version.json dist\version.json >nul
)
echo [OK] dist 目录: comfy_launcher.exe + core.py + version.json
pause
