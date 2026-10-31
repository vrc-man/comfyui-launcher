@echo off
rem 一键发布：打包 exe -> 自动递增内核版本 -> 生成 version.json（双哈希）-> 同步 dist 与仓库本体
rem
rem 用法：
rem   build.bat                         直接打包，版本号自动 +1，本体按 raw 方式随仓库提交
rem   build.bat "更新说明"               同时把说明写进 version.json 的 changelog
rem   版本号来源：core.py 里的 CORE_VERSION（本脚本会自动把它补丁号 +1）
chcp 65001 >nul
cd /d "%~dp0"

set "VENV=%~dp0comfyui-launcher-venv"
if not exist "%VENV%\Scripts\pyinstaller.exe" (
    echo [ERROR] pyinstaller not found, run create_venv.bat first
    pause
    exit /b 1
)

echo [1/3] 打包 exe ...
"%VENV%\Scripts\pyinstaller.exe" --onefile --windowed --name comfy_launcher --clean --noconfirm ^
  --collect-submodules uvicorn --collect-submodules fastapi --collect-submodules starlette --collect-submodules anyio ^
  --icon "%~dp0comfy_launcher.ico" comfy_launcher.py
if errorlevel 1 (
    echo [ERROR] build failed
    pause
    exit /b 1
)
echo [OK] dist\comfy_launcher.exe

echo.
echo [2/3] 递增版本号并生成 version.json（core.py / exe 双哈希）...
"%VENV%\Scripts\python.exe" make_version.py --bump --raw --changelog="%~1"
if errorlevel 1 (
    echo [ERROR] make_version.py failed
    pause
    exit /b 1
)

echo.
echo [3/3] 完成。分发目录 dist 内容：
dir /b dist
echo.
echo 接下来手动提交（exe 与 core.py、version.json 必须一起推）：
echo     git add -A ^&^& git commit -m "release" ^&^& git push origin main
echo.
pause
