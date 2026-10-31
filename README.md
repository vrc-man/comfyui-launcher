# ComfyUI 启动器

当前版本 **v1.0.7**（以仓库根的 `version.json` 为准；核心逻辑版本见 `core.py` 的 `CORE_VERSION`）。

一个带图形界面（GUI）的 ComfyUI 启动/管理工具，内置本地 HTTP 管理服务，可被其他程序远程调用（如网页管理面板），并支持通过 GitHub 镜像热更新（核心逻辑与启动器本体都能自更新）。

## 直接使用（不用编译，下载现成的就行）

作者已经在仓库里放好了构建产物，**不需要装 Python、不需要 pip、不需要打包**：

| 下载这两个文件（必需） |
|---|
| [`comfy_launcher.exe`](https://raw.githubusercontent.com/vrc-man/comfyui-launcher/main/comfy_launcher.exe) —— 启动器本体，约 17 MB |
| [`core.py`](https://raw.githubusercontent.com/vrc-man/comfyui-launcher/main/core.py) —— 核心逻辑，壳启动时要加载它 |

可选再下一个 [`version.json`](https://raw.githubusercontent.com/vrc-man/comfyui-launcher/main/version.json)，只是方便肉眼查看当前版本号，不放也能正常用。

步骤：

1. 把 `comfy_launcher.exe` 和 `core.py` 放在**同一个目录**（例如 `D:\ComfyLauncher\`）
2. 双击 `comfy_launcher.exe`
3. 在窗口上方「设置」里填两个路径（改完自动保存，下次不用再填）：
   - **ComfyUI 目录**：包含 `main.py` 的那个目录，例如 `D:\ComfyUI_windows_portable\ComfyUI`
   - **Python 路径**：便携版一般是 `D:\ComfyUI_windows_portable\python_embeded\python.exe`
4. 点「🚀 启动」拉起 ComfyUI；之后用「⏹ 停止 / 🔄 重启」管理，日志在下方两个标签页里实时滚动

首次运行会在 exe 同目录自动生成 `comfy_launcher_config.json`（内含随机生成的 API 密钥），**不要删它**——密钥就在里面。

> 提示：ComfyUI 本身不支持含中文或空格的路径，请把它放在纯英文无空格目录（如 `D:\ComfyUI`），否则启动器会直接报错拦住。

想升级时不用重新下载：点窗口里的「📥 检查更新」，核心逻辑和启动器本体都会自动换成最新版（本体更新后会自动重启）。

## 功能特性

- **图形界面管理**：启动 / 停止 / 重启 ComfyUI，实时查看启动器与 ComfyUI 的双日志
- **版本可见**：顶栏与状态栏直接显示**内核版本**（`core.py` 里的 `CORE_VERSION`）和本体标识（exe 名 + 哈希前 8 位 / 源码模式），自己跑的是哪版一目了然
- **启动选项开关**：fp16_accumulation、cuda_malloc、自定义命令、自定义环境变量，逐项独立控制，界面实时显示「实际参数」
- **本地管理 API**：FastAPI 服务，支持 `status / logs / start / stop / restart`，`X-Key` 密钥鉴权，仅监听 127.0.0.1
- **三线程分离**：UI / HTTP 服务 / 子进程管理互不阻塞
- **安全可控**：只操作 8188 端口相关进程树，停止/重启需二次确认（`X-Confirm`）
- **热更新机制**：核心逻辑在独立的 `core.py`，通过 GitHub 镜像对比哈希自动/手动更新，无需重新打包 exe
- **路径引导**：未配置或填错 ComfyUI / Python 目录时给出明确中文提示

## 启动选项

GUI 第二栏「启动选项」四项，勾选即写入配置、下次启动 ComfyUI 时生效：

| 选项 | 效果 |
|---|---|
| 开启 fp16_accumulation | 向 `main.py` 追加 `--fast fp16_accumulation` |
| 开启 cuda_malloc | 勾选追加 `--cuda-malloc`；**取消勾选追加 `--disable-cuda-malloc`**（torch ≥ 2.0 上 cudaMalloc 默认开启，不显式传反向参数等于没关） |
| 开启自定义命令 | 把「例：--port 8188 --lowvram ...」框里的参数原样追加（支持 `"带空格的路径"`） |
| 开启自定义环境变量 | 每行 `KEY=VALUE` 注入子进程环境，覆盖同名系统变量（`#` 开头为注释） |

配置里的 `comfyui.args` 是**基础参数**，两个开关托管的 `--cuda-malloc` / `--fast fp16_accumulation` 会自动从基础参数里剔除，避免关掉开关却仍被子里的旧参数打开（老配置升级时自动迁移）。界面「实际参数」一行就是最终下发的真实 argv。

## 目录结构

```
comfyui-launcher/
├── comfy_launcher.py        # 壳（可执行入口，打包为 exe；含 PyInstaller 依赖锚点）
├── comfy_launcher.exe       # 打包好的启动器本体（raw 方式分发，哈希与 version.json 一致）
├── core.py                  # 核心逻辑（可热替换，更新即覆盖此文件）
├── make_version.py          # 发布工具：生成 version.json（版本 + 哈希）
├── version.json             # 版本清单（更新对比基准）
├── build.bat                # 打包 exe（自动生成 version.json 并复制 core.py）
├── create_venv.bat          # 创建虚拟环境 + 安装 PyInstaller / FastAPI
├── start_launcher.bat       # 源码模式启动
├── comfy_launcher.ico       # 应用程序图标（打包用）
├── comfyui启动器图标.png    # 图标原图 2048×2048（豆包 AI 生成）
└── icon_preview.png         # 图标预览 256×256
```

## 开发 / 打包

只改 `core.py` 时不用重新打包（热更新会替换它）；改了 `comfy_launcher.py`（壳）才必须重打包。

```bat
:: 首次：创建 venv（用 I:\cc\Py64-311 等 base python）
create_venv.bat

:: 一条命令搞定：打包 exe -> 内核版本自动 +1 -> 重生成 version.json（core+exe 双哈希）
::              -> 同步 dist\ 与仓库根的本体
build.bat "本次更新说明"
```

`build.bat` 现在全自动，不需要再手动跑 `make_version.py`：

1. 用 PyInstaller 打包 `dist\comfy_launcher.exe`
2. 把 `core.py` 的 `CORE_VERSION` **补丁号 +1**（单一版本来源，不用手改两处）
3. 用新版本号重生成 `version.json`：`core_sha256`、`core_size`、`exe.sha256`、`exe.size`、`changelog`
4. 把 `dist\comfy_launcher.exe` 同步成仓库根的 `comfy_launcher.exe`（raw 分发用，哈希与实际提交文件一致）
5. 把 `core.py` / `version.json` 同步回 `dist\`，保证打包目录永远跟仓库一致

> 注意：`build.bat` 会重打包 exe，**哈希必然变**，已发布的用户点更新时会连 17MB 本体一起下。
> 只改了 `core.py` 想省流量的话，别跑 `build.bat`，直接 `python make_version.py --bump --raw --changelog="说明"`（只重生成清单，不动 exe）。

打包产物在 `dist\`：`comfy_launcher.exe` + `core.py` + `version.json`。

> `.bat` 文件必须是 **CRLF** 行尾，否则 cmd 会解析错位（报 `'et' 不是内部或外部命令` 这类怪错）。
> 仓库里已用 `.gitattributes` 的 `*.bat -text` 锁住，克隆下来不会被打回 LF。

## 更新机制

两条独立通道，都在「📥 检查更新」里一起检测：
| 通道 | 对比对象 | 更新方式 |
|---|---|---|
| **核心逻辑** `core.py` | 本地 `core.py` 的 sha256 ↔ 远端 `version.json` 的 `core_sha256` | 下载 → 校验 → 备份 `core.py.bak` → 原子替换 → 重启，**不用重新下载 exe** |
| **启动器本体** `comfy_launcher.exe` | 本地正在运行的 exe 的 sha256 ↔ 远端 `version.json` 的 `exe.sha256` | 下载到 `<exe>.new` → 校验 → 后台脚本等进程退出后改名替换 → 拉起新版 → 清理 `.old` |

- 远端 `version.json` 里 `version` 比本地旧时**一律跳过**（防降级，避免把本地改动冲掉）
- 用户自己改过 `core.py` / 换过 exe，哈希必然不同 → 会提示有更新，点更新会覆盖（`core.py.bak` 留回滚点）
- 默认**不自动检测**，需手动点按钮，或在 GUI 勾选「启动自动检测更新」
- 更新源：GitHub 直连 + `gh-proxy.com` / `ghproxy.net` 两个加速镜像，任一可用即可

### 用本地代理加速（可选）

如果自己有代理（Clash / V2Ray / SS 等），可以在「设置 → 更新代理」填 **IP + 端口**，之后查更新、下 `core.py`、下 exe 全部走这个代理直连 GitHub 官方地址，通常比第三方镜像更稳更快：

- 填法：地址 `127.0.0.1`、端口 `7890`（Clash 的混合端口常见值；V2Ray 常见 `10809`/`10808`）
- 右侧「测试」按钮会立刻用它拉一次 GitHub 官方 `version.json`，把耗时和远端版本号弹出来，填错会直接报错（例如目标计算机积极拒绝 = 端口不通）
- **留空 = 不强制代理**（沿用系统代理设置/直连），行为与旧版一致
- 只支持 `http://` 代理，`socks5://` 需要额外装 PySocks，填了会提示格式不对，不会假装成功
- 配置里对应 `update.proxy`，写 `127.0.0.1:7890` 或 `http://127.0.0.1:7890` 都认

### 发版流程（重要）

exe 有两种放法，二选一：

**A. 仓库内文件（和以前更新 core.py 完全一样的做法，最省事）**

```bat
build.bat
python make_version.py 1.0.5 "更新说明" raw    :: 会把 dist\comfy_launcher.exe 复制成仓库根的 comfy_launcher.exe
git add core.py version.json comfy_launcher.exe comfy_launcher.py make_version.py build.bat README.md
git commit -m "release: v1.0.5"
git push origin main
```

下载地址自动写成 `https://raw.githubusercontent.com/<repo>/main/comfy_launcher.exe`，
镜像会做基址替换（`gh-proxy.com/https://raw.githubusercontent.com/...`），不用你操心。

> 代价：每次发版都在 git 历史里存一份 ~18MB 二进制。发 10 个版本仓库长到 ~180MB，clone 会变慢。
> 介意的话用方案 B，或者 `git rm --cached` 后配合 Release。

**B. Release 附件（仓库只放文本）**

```bat
build.bat
python make_version.py 1.0.5 "更新说明"        :: exe.url 自动指向 releases/download/v1.0.5/
git add core.py version.json && git commit -m "release: v1.0.5" && git push origin main
gh release create v1.0.5 "dist\comfy_launcher.exe" --title "v1.0.5" --notes "说明"
```

也可以指定任意直链：`python make_version.py 1.0.5 "说明" "https://你的服务器/comfy_launcher.exe"`

**共同注意**

- 用户端换 exe 前会校验 sha256，所以**「被上传的文件」必须就是 `make_version.py` 哈希的那份**。打完包又改了代码 → 重新打包后必须重跑 `make_version.py`（raw 模式会自动同步仓库里那份）
- 只改了 `core.py` 的版本：不用重新打包、不用动 `exe` 字段，直接推 `core.py` + `version.json`
- 改了 `comfy_launcher.py`（壳）却不重打包 → 老用户的旧 exe 拿不到壳的修复（只有 `core.py` 能热更新）

## 管理 API

本地 `http://127.0.0.1:18808`（端口可在 GUI 配置），请求头 `X-Key` 鉴权：

| 方法 | 路径 | 说明 | 额外要求 |
|---|---|---|---|
| GET | `/api/health` | 健康探针：返回 `pid` / 实例号 / 版本，用于确认端口上跑的是哪个实例 | 无（仅本机） |
| GET | `/api/status` | 运行状态 / PID / 端口占用 | X-Key |
| GET | `/api/logs?tail=N&source=launcher\|comfy` | 最近 N 行日志（分来源） | X-Key |
| POST | `/api/start` | 启动 ComfyUI | X-Key |
| POST | `/api/stop` | 停止 ComfyUI | X-Key + X-Confirm |
| POST | `/api/restart` | 重启 ComfyUI | X-Key + X-Confirm |

## 已知行为与限制

- **改端口 / 改密钥需要重启启动器**：HTTP 服务在启动时绑定端口，GUI 里改「管理端口」只写配置，界面底部会提示「重启启动器后生效」；密钥改动立即生效（鉴权走内存缓存）。
- **密钥以明文存于 `comfy_launcher_config.json`**（该文件已被 gitignore），请勿把分发目录连同配置一起打包传播。
- **ComfyUI 输出编码**：启动子进程时强制 `PYTHONIOENCODING=utf-8`/`PYTHONUTF8=1`，避免 GBK 输出在日志面板里乱码。
- **端口强占处理**：启动前若目标端口被占用，会 `taskkill /T /F` 杀掉监听该端口的进程树（仅按端口定位 PID，绝不按进程名批量杀）。
- **单实例**：靠 `comfy_launcher.lock` 的 `msvcrt` 文件锁实现，第二个实例只弹提示后退出。
- **GUI 日志面板**最多保留 4000 行，热更新前会先写 `.tmp` 再原子替换 `core.py`（`core.py.bak` 为回滚点）。

## 变更记录

### v1.0.12

- **修 UI 卡顿（关键性能问题）**：日志面板原来每插一行都调 `text.tag_config(...)`，tkinter 会把该 tag 重新应用到全文（O(n²)）——实测 1500 行要 **23 秒**（65 行/秒），ComfyUI 一刷日志界面就冻住。改为 tag 只配置一次 + 每批日志一次 `insert` + 按行 `tag_add`：**99,087 行/秒**，3000 行实测 1.13 秒渲染完，UI 定时器抖动仅 1ms
- ComfyUI 日志落盘改为**攒批写**（8KB 或 0.6 秒触发一次，另有后台兜底线程），不再是每行 open/write/close：吞吐 3,753 → 66,367 行/秒
- 日志滚动更聪明：只有当你贴着底部时才自动跟着滚，往上翻看历史时不会被新日志拽回去
- 启动时后台预热本体 exe 的哈希（18MB），避免首帧在主线程上算哈希造成一次性卡顿

### v1.0.11

- 「实时日志」右上角新增 **🧹 清空日志** 按钮：一次清掉两个页签的显示、内存环形缓冲（`/api/logs` 也会随之变空）和日志文件（`comfy_launcher.log` / `comfyui.log`），清完留一行提示
- 清空只影响显示与日志文件，**不会动 ComfyUI 进程**；日志队列里还没渲染的旧行也会一并丢弃，避免清完又被填回去

### v1.0.10

- 点「📥 检查更新」后弹出**置顶小窗「正在检测新版本，请稍等…」**（带动画进度条），核对完自动关闭，再弹出结果/确认框；超过 1.5 秒会提示「网络较慢，仍在检测」
- 修：worker 线程里直接调 `root.after(...)` / `messagebox`，tkinter 会抛 `main thread is not in main loop` 并被 `except` 吞掉 —— 表现为弹窗关不掉、日志不刷新、按钮点了没反应。改为线程安全 UI 队列（主线程每 50ms 泵出执行）
- 性能：版本清单改为**多源并行**拉取，两个源版本一致即采用，发现比本地新的版本立刻返回；检查耗时从约 8 秒降到约 1 秒（串行时一个慢镜像就能拖到 8.4s）
- 清单单源超时收紧到 5 秒（原来是 15 秒），慢源不再阻塞整体

### v1.0.9

- GUI 顶栏显示 **内核版本**（`core.py` 的 `CORE_VERSION`），底部状态栏同时给出内核版本与本体标识（`本体 comfy_launcher.exe (哈希前8位)` / `本体 源码模式`）
- `build.bat` 全自动发版：打包 → 内核版本补丁号 +1 → 重生成 `version.json`（core + exe 双哈希 + changelog）→ 同步 `dist\` 与仓库根本体
- `make_version.py` 支持显式 flag：`--bump[=patch|minor|major]`、`--raw`、`--changelog=文本`；位置参数老用法仍兼容
- 修：`.bat` 文件是裸 LF 导致 cmd 解析错位（报 `'et' 不是内部或外部命令`），已全部转 CRLF 并加 `.gitattributes`（`*.bat -text`）锁定

### v1.0.8

- 新增**自定义 HTTP 代理**：设置里填 IP + 端口后，查更新 / 下 `core.py` / 下 exe 全部走该代理直连 GitHub 官方地址
- 新增「测试」按钮：当场用它拉一次官方 `version.json`，显示耗时与远端版本；不通会明确报错
- `update.proxy` 留空 = 不强制代理（沿用系统代理/直连），行为与旧版一致；`socks5://` 会提示格式不支持而不是假装成功

### v1.0.7

- 修：**版本清单与文件做配对校验**。所有源（GitHub 直连 + 两个镜像）的 `version.json` 和 `core.py` / `exe` 都取一遍，只要能找到「某份清单的哈希 == 某份下载内容」的配对就采用
  - 背景：raw CDN 各对象缓存独立，刚发版时常出现「清单还是旧版、文件已刷新」或反之，旧实现会误报 `sha256 校验失败` 或误判「已是最新」
  - 现在的失败提示也改成「清单与文件不同步（CDN 缓存未刷新），请稍后重试」

### v1.0.6

- 修：版本清单改为**多源取最高版本**（原来第一个源成功就返回，遇到 CDN 缓存旧清单会持续几分钟误判「已是最新」）
- 修：HTTP 服务存活探测改为 `/api/health` 校验实例号，不再被「另一个启动器实例的 403」骗到，端口被占用时会明确指出是另一个实例

### v1.0.5

- 新增**启动器本体自更新**：`version.json` 增加 `exe.sha256 / exe.size / exe.url`；老用户点「检查更新」会同时看到「核心逻辑」和「启动器本体」两项，本体更新会下载新 exe、校验哈希、由后台脚本等进程退出后替换文件并自动拉起新版
  - 替换脚本用「反复尝试改名」判断旧进程是否放开 exe：`tasklist` 在无控制台的分离进程里会直接挂死，这条坑已踩过并绕开
  - 失败时自动把旧 exe 换回来照常启动，绝不让用户既没新版又没启动器；`*_launcher.exe.old` / `.new` / 脚本残留下次启动自动清理
- exe 支持两种托管：仓库内文件（`make_version.py ... raw`，和更新 core.py 一样直接覆盖提交）与 Release 附件；镜像前缀规则按两种地址分别处理
- 修：Release 模式下的镜像前缀此前从未生效（判断条件把所有镜像都跳过了），现在按代理根正确拼接
- 更新源增加 `raw.githubusercontent.com` 直连（放在镜像之前，有代理的用户直连更稳）
- 防降级：远端版本比本地旧时两条通道都跳过（本地改过 `core.py` 也不会被远端旧版覆盖）
- `make_version.py` 自动带上 exe 哈希与下载地址，raw 模式还会把 dist 的 exe 同步成仓库里那份，避免「上传的」和「哈希的」不是同一字节

### v1.0.4

- 修：`cuda_malloc` 开关语义。原来取消勾选只是「不传 `--cuda-malloc`」，而 ComfyUI 在 torch ≥ 2.0 上默认就开 cudaMallocAsync，等于没关。现在取消勾选会显式追加 `--disable-cuda-malloc`（`comfy/cli_args.py` L56 的反向开关）
- 迁移：老配置 `args` 里写了 `--disable-cuda-malloc` 的，加载时自动把开关置为关闭，不会出现「显示已开启、实际被禁用」
- 两个托管参数（`--cuda-malloc` / `--disable-cuda-malloc`）都会从基础 `args` 剥离，避免与开关叠加

### v1.0.3

- 新增「启动选项」区：**fp16_accumulation / cuda_malloc / 自定义命令 / 自定义环境变量** 四个独立开关
  - 两个托管参数从 `comfyui.args` 里剥离，改由开关追加，老配置自动迁移（升级后行为不变）
  - 自定义命令用自写解析器（不用 `shlex`），`--output-directory "D:\out dir"` 这类 Windows 路径不会被反斜杠转义吃掉
  - 自定义环境变量逐行 `KEY=VALUE` 校验注入，与应用自身强制的 `PYTHONIOENCODING/PYTHONUTF8` 不冲突
- 新增「实际参数」实时预览，勾选后立刻看到最终下发的 argv
- 调整布局：操作按钮固定在状态栏下方，日志区再长也不会把按钮挤出可视区

### v1.0.2

- 修：单实例锁失效——锁文件句柄原先留在局部变量里，函数返回即被 GC 关闭，导致可多开
- 修：配置非原子写——GUI 每敲一个字符就整体重写配置，API 线程可能读到半截文件并回落默认值，进而静默重建 `api_key`，造成 GUI 显示的密钥与真实密钥不一致（403 排查困难）
- 修：配置里 `api_key` 为空时不再自动重建，仅在首次运行（配置文件不存在）时生成
- 修：`restart` 竞态——`stop()` 结束进程树后未 `wait()` 回收子进程，`start()` 可能误判「已在运行」而跳过一次重启
- 修：热更新替换顺序——原实现先把 `core.py` 移成 `.bak` 再写新文件，中途失败会留下「没有 core.py」的不可启动状态；改为先写 `.tmp` 校验、再备份、最后原子替换
- 修：`build.bat` 只在 `dist\version.json` 缺失时才复制，导致重打包后分发目录带着旧哈希（假更新/漏更新）；改为每次覆盖
- 修：`create_venv.bat` 未安装 `fastapi`/`uvicorn`，新环境按文档建 venv 后打包会缺运行期依赖
- 改进：PyInstaller 增加 `--collect-submodules uvicorn/fastapi/starlette/anyio`，避免 `core.py` 运行时动态加载导致的隐性 ImportError
- 改进：GUI 设置保存改为 400ms 防抖 + 关窗强制落盘；日志面板加上限；底部管理地址显示真实绑定端口

## 致谢

- 应用图标由豆包（Doubao）AI 创作生成。

## 说明

本项目**全部代码由 DeepSeek 全程构建**——包括 `core.py`（GUI + 管理 API + 进程管理 + 自更新）、`comfy_launcher.py`（壳）、`make_version.py`（发布工具）、`build.bat` / `create_venv.bat` / `start_launcher.bat` 构建脚本，以及本文档的内容整理与校订。

日常使用中如遇问题，把界面「实时日志」里的报错贴出来即可定位。
