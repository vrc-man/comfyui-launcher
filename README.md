# ComfyUI 启动器

一个带图形界面（GUI）的 ComfyUI 启动/管理工具，内置本地 HTTP 管理服务，可被其他程序远程调用（如网页管理面板），并支持通过 GitHub 镜像热更新核心逻辑。

## 功能特性

- **图形界面管理**：启动 / 停止 / 重启 ComfyUI，实时查看启动器与 ComfyUI 的双日志
- **本地管理 API**：FastAPI 服务，支持 `status / logs / start / stop / restart`，`X-Key` 密钥鉴权，仅监听 127.0.0.1
- **三线程分离**：UI / HTTP 服务 / 子进程管理互不阻塞
- **安全可控**：只操作 8188 端口相关进程树，停止/重启需二次确认（`X-Confirm`）
- **热更新机制**：核心逻辑在独立的 `core.py`，通过 GitHub 镜像对比哈希自动/手动更新，无需重新打包 exe
- **路径引导**：未配置或填错 ComfyUI / Python 目录时给出明确中文提示

## 目录结构

```
comfyui-launcher/
├── comfy_launcher.py        # 壳（可执行入口，打包为 exe；含 PyInstaller 依赖锚点）
├── core.py                  # 核心逻辑（可热替换，更新即覆盖此文件）
├── make_version.py          # 发布工具：生成 version.json（版本 + 哈希）
├── version.json             # 版本清单（更新对比基准）
├── build.bat                # 打包 exe（自动生成 version.json 并复制 core.py）
├── create_venv.bat          # 创建虚拟环境 + 安装 PyInstaller
├── start_launcher.bat       # 源码模式启动
└── comfy_launcher.ico       # 应用程序图标
```

## 使用（分发模式）

把 `comfy_launcher.exe` 和 `core.py` 放在**同一目录**，双击 exe 即可：

1. 在「设置」里填写你的 ComfyUI 目录和 Python 路径
   - ComfyUI 目录：包含 `main.py` 的目录
   - Python 路径：通常为 `python_embeded\python.exe`（ComfyUI 便携版）
2. 点「🚀 启动」拉起 ComfyUI，或点「⏹ 停止 / 🔄 重启」管理
3. 实时日志在两个标签页分别显示（启动器/API 与 ComfyUI）

> 提示：ComfyUI 不支持含中文或空格的路径，请放在纯英文无空格目录（如 `D:\ComfyUI`）。

## 开发 / 打包

```bat
:: 首次：创建 venv（用 I:\cc\Py64-311 等 base python）
create_venv.bat

:: 打包 exe + 生成 version.json + 复制 core.py 到 dist
build.bat
```

打包产物在 `dist\`：`comfy_launcher.exe` + `core.py` + `version.json`。

## 更新机制

- 每次改完 `core.py` 后运行 `python make_version.py [新版本号] "更新说明"`，生成 `version.json`
- 提交推送后，用户启动器即可对比哈希，发现不一致时自动下载新 `core.py` 覆盖（`core.py.bak` 备份回滚）
- 启动器默认**不自动检测更新**，可在 GUI 勾选「启动自动检测更新」或手动点「📥 检查更新」

## 管理 API

本地 `http://127.0.0.1:18808`（端口可在 GUI 配置），请求头 `X-Key` 鉴权：

| 方法 | 路径 | 说明 | 额外要求 |
|---|---|---|---|
| GET | `/api/status` | 运行状态 / PID / 端口占用 | X-Key |
| GET | `/api/logs?tail=N&source=launcher\|comfy` | 最近 N 行日志（分来源） | X-Key |
| POST | `/api/start` | 启动 ComfyUI | X-Key |
| POST | `/api/stop` | 停止 ComfyUI | X-Key + X-Confirm |
| POST | `/api/restart` | 重启 ComfyUI | X-Key + X-Confirm |

## 致谢

- 应用图标由豆包（Doubao）AI 创作生成。
