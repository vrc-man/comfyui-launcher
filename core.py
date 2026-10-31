# -*- coding: utf-8 -*-
"""
ComfyUI 启动器（独立项目，tkinter GUI + 本地 HTTP 管理服务二合一）。

三线程架构（互不阻塞）：
  - 主线程   : tkinter UI 事件循环，只通过 queue.Queue 读状态/日志
  - 线程 B   : HTTP 管理服务（http.server，默认 127.0.0.1:18808，端口可自定义）
  - 线程 C   : ComfyUI 子进程管理（Popen / taskkill / 日志采集）

行为要点：
  - 启动自检：8188 被占用则强制杀掉占用进程树再启动（保证完全接管）
  - 杀进程只针对 8188 监听 PID 及其子进程树，从不按名称/通配杀
  - stop / restart 需请求头 X-Confirm（confirm_key）双重确认，防误杀
  - 仅监听 127.0.0.1，非本机请求一律拒绝

依赖：仅 Python 标准库（tkinter / http.server / subprocess / threading）。
"""

import os
import sys
import json
import time
import queue
import socket
import shutil
import secrets
import hashlib
import threading
import subprocess
import http.server
import re
from pathlib import Path
from typing import Optional
from collections import deque
from datetime import datetime

# 打包（--onefile）时 __file__ 指向临时解压目录，需用 exe 所在目录定位配置/日志
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "comfy_launcher_config.json"
LOG_PATH = BASE_DIR / "comfy_launcher.log"       # 启动器/API 操作日志
COMFY_LOG_PATH = BASE_DIR / "comfyui.log"        # ComfyUI 输出日志
LOG_QUEUE = queue.Queue()          # (ts_str, source, level, line) 线程安全；source in ("launcher","comfy")
LOG_RING = deque(maxlen=500)       # 环形缓冲：launcher 流（启动器/API 操作）
COMFY_RING = deque(maxlen=1000)    # 环形缓冲：comfy 流（ComfyUI 输出）
_log_lock = threading.Lock()

DEFAULT_CONFIG = {
    "listen_host": "127.0.0.1",
    "listen_port": 18808,
    "api_key": "",
    "confirm_key": "",
    # 启动器启动后是否自动启动 ComfyUI（true=自动拉起；false=仅启动管理服务，手动点「启动」）
    "auto_start_comfyui": False,
    # 启动时自动检测更新（可 GUI 勾选；默认关闭，仅用「检查更新」按钮手动更新）
    "update": {"auto": False},
    "comfyui": {
        "comfyui_path": "",
        "python_path": "",
        "port": 8188,
        # 基础参数（固定部分；--fast fp16_accumulation / --cuda-malloc 由下面的开关单独控制）
        "args": [
            "--windows-standalone-build",
            "--disable-auto-launch",
            "--preview-method", "auto",
        ],
        # ── 启动可选项（GUI 勾选，互不干扰）──
        "use_fp16_accumulation": True,   # 追加 --fast fp16_accumulation
        "use_cuda_malloc": True,         # 追加 --cuda-malloc
        "enable_custom_args": False,     # 是否启用「自定义命令」
        "custom_args": "",               # 自定义参数文本（空格分隔，支持 "带空格的路径"）
        "enable_custom_env": False,      # 是否启用「自定义环境变量」
        "env_lines": [],                 # ["KEY=VALUE", ...]；# 开头为注释
    },
}

# 由开关托管、不再从 args 里重复透传的参数
_MANAGED_ARGS = ("--cuda-malloc", "--disable-cuda-malloc")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def split_args(text: str) -> list:
    """把自定义参数文本拆成 argv。

    不用 shlex：Windows 路径里的反斜杠会被 shlex 当转义吃掉（D:\\ComfyUI -> D:ComfyUI）。
    这里只认成对的单/双引号，反斜杠原样保留。
    """
    out, cur, quote = [], "", None
    for ch in str(text or ""):
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in ("'", '"'):
            quote = ch
        elif ch.isspace():
            if cur:
                out.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def strip_managed_args(raw_args) -> list:
    """剔除基础 args 里由开关托管的参数（--cuda-malloc / --fast fp16_accumulation）。"""
    out, i = [], 0
    raw = [str(a) for a in (raw_args or [])]
    while i < len(raw):
        a = raw[i]
        if a in _MANAGED_ARGS:
            i += 1
            continue
        if a == "--fast" and i + 1 < len(raw) and raw[i + 1].lower() == "fp16_accumulation":
            i += 2
            continue
        if a.lower().startswith("--fast=") and "fp16_accumulation" in a.lower():
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def build_args(comfyui_cfg: dict) -> list:
    """组装最终传给 main.py 的参数：基础 args + 开关追加 + 自定义命令。"""
    cu = comfyui_cfg or {}
    # 先剔除历史配置里写的托管参数，避免开关关掉后仍被子里的 args 打开
    base = strip_managed_args(cu.get("args"))

    # cudaMalloc 在 torch>=2.0 上是 ComfyUI 的默认行为，所以「关闭」必须显式传反面参数，
    # 只靠「不传 --cuda-malloc」是关不掉的。
    if cu.get("use_cuda_malloc", True):
        base.append("--cuda-malloc")
    else:
        base.append("--disable-cuda-malloc")
    if cu.get("use_fp16_accumulation", True):
        base += ["--fast", "fp16_accumulation"]
    if cu.get("enable_custom_args"):
        base += split_args(cu.get("custom_args") or "")
    return base


def build_env_lines(env_lines) -> dict:
    """解析自定义环境变量行（KEY=VALUE）。非法行/TYPE 不对的行直接忽略。"""
    env = {}
    for raw in env_lines or []:
        s = str(raw).strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip()
        if not _ENV_KEY_RE.match(k):
            continue
        env[k] = v
    return env


def _now():
    return datetime.now().strftime("%H:%M:%S")


# ── 通用目录提示（placeholder，输入框为空时灰色显示）──
PLACEHOLDER_COMFY = r"例如：D:\ComfyUI_windows_portable\ComfyUI"
PLACEHOLDER_PY = r"例如：D:\ComfyUI_windows_portable\python_embeded\python.exe"


def _log(level, line, source="launcher"):
    """统一日志（默认 launcher 流）。ComfyUI 输出用 _log(..., source="comfy")。

    两条独立流：
      - launcher：启动器自身 + API 操作记录 -> LOG_RING + comfy_launcher.log
      - comfy   ：ComfyUI 子进程 stdout/stderr -> COMFY_RING + comfyui.log
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] [{level}] {line}"
    with _log_lock:
        if source == "comfy":
            COMFY_RING.append(entry)
            try:
                with open(COMFY_LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
                    f.write(entry + "\n")
            except OSError:
                pass
        else:
            LOG_RING.append(entry)
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(entry + "\n")
            except OSError:
                pass
    LOG_QUEUE.put((_now(), source, level, entry))


# ── 配置读写 ──
_config_lock = threading.Lock()
# 鉴权密钥内存缓存：避免每个 API 请求反复读盘/误触发密钥重建
_AUTH = {"api_key": "", "confirm_key": ""}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    first_run = not CONFIG_PATH.is_file()
    try:
        if CONFIG_PATH.is_file():
            # utf-8-sig：兼容记事本 / Windows PowerShell 5.1 写出的 UTF-8 BOM。
            # 带 BOM 时用 utf-8 解析会直接抛错，进而整份配置被回落成默认值（密钥看起来"丢了"）。
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            for k in cfg:
                if k in saved:
                    cfg[k] = saved[k]
            cu = cfg["comfyui"]
            su = saved.get("comfyui", {})
            for k in cu:
                if k in su:
                    cu[k] = su[k]
            # ── 老配置迁移 ──
            # 旧版把 --fast fp16_accumulation / --cuda-malloc 直接写死在 args 里，
            # 新版交给开关托管：配置里没有这两个键时，按旧 args 的实际情况推断，
            # 保证升级后默认行为和升级前一模一样。
            legacy = [str(a).lower() for a in (su.get("args") or [])]
            if "use_fp16_accumulation" not in su:
                cu["use_fp16_accumulation"] = (
                    "fp16_accumulation" in legacy
                    or any(a.startswith("--fast=") and "fp16_accumulation" in a for a in legacy)
                )
            if "use_cuda_malloc" not in su:
                # 老配置里若显式写了 --disable-cuda-malloc，迁移后开关应为「关闭」
                cu["use_cuda_malloc"] = ("--disable-cuda-malloc" not in legacy)
            if "env_lines" not in su:
                cu["env_lines"] = []
            if "custom_args" not in su:
                cu["custom_args"] = ""
            # 顺手把 args 里的托管参数摘掉，配置文件和「实际参数」预览保持一致
            cu["args"] = strip_managed_args(cu.get("args"))
    except Exception as e:
        _log("WARN", f"配置读取失败，使用默认: {e}")
    # 密钥兜底：仅首次运行（配置文件不存在）自动生成；
    # 配置已存在但密钥为空时保留空值，绝不静默改写（否则鉴权与 GUI 显示会错位）
    changed = False
    if first_run:
        if not cfg.get("api_key"):
            cfg["api_key"] = secrets.token_hex(16)
            changed = True
        if not cfg.get("confirm_key"):
            cfg["confirm_key"] = secrets.token_hex(16)
            changed = True
        if changed:
            save_config(cfg)
    _AUTH["api_key"] = cfg.get("api_key", "")
    _AUTH["confirm_key"] = cfg.get("confirm_key", "")
    return cfg


def save_config(cfg: dict):
    """原子写配置：先写 .tmp 再 os.replace，避免其他线程读到半截/空文件。"""
    with _config_lock:
        try:
            tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
            tmp.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, CONFIG_PATH)
            # 同步鉴权缓存：GUI 改完密钥立即生效，不必重启启动器
            _AUTH["api_key"] = str(cfg.get("api_key", "") or "")
            _AUTH["confirm_key"] = str(cfg.get("confirm_key", "") or "")
        except OSError as e:
            _log("ERROR", f"配置保存失败: {e}")


def reload_config() -> dict:
    return load_config()


# ── 进程管理 ──
class ComfyManager:
    """ComfyUI 子进程管理，跨线程安全。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._exit_code: int | None = None
        self._port = DEFAULT_CONFIG["comfyui"]["port"]
        self._last_action = ""

    # ── 端口 / 进程探测 ──
    @staticmethod
    def _port_in_use(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    def _pid_on_port(port: int):
        """返回监听指定端口的 PID（无则 None）。用 netstat 精确解析。"""
        try:
            out = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True,
                encoding="gbk", errors="replace", timeout=5,
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0] == "TCP":
                    addr = parts[1]
                    state = parts[3] if len(parts) > 3 else ""
                    if state == "LISTENING" and addr.endswith(f":{port}"):
                        try:
                            return int(parts[-1])
                        except ValueError:
                            continue
        except Exception as e:
            _log("WARN", f"netstat 解析失败: {e}")
        return None

    @staticmethod
    def _kill_tree(pid: int):
        """强制结束进程树。只针对指定 PID，绝不按名称杀。"""
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=15,
            )
            return True
        except Exception as e:
            _log("ERROR", f"结束进程 {pid} 失败: {e}")
            return False

    # ── 状态 ──
    def status(self) -> dict:
        with self._lock:
            proc = self._proc
            running = proc is not None and proc.poll() is None
            pid = proc.pid if (proc is not None and running) else None
            exit_code = self._exit_code if not running else None
            started_at = self._started_at if running else None
            last_action = self._last_action
            port = self._port
        return {
            "running": running,
            "pid": pid,
            "started_at": started_at,
            "exit_code": exit_code,
            "port": port,
            "port_in_use": self._port_in_use(port),
            "last_action": last_action,
        }

    # ── 启动 ──
    def start(self, cfg: dict = None) -> dict:
        cfg = cfg or load_config()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return {"ok": True, "message": "已在运行，无需重复启动", "pid": self._proc.pid}
            cu = cfg.get("comfyui") or {}
            comfyui_path = str(cu.get("comfyui_path") or "").strip()
            python_path = str(cu.get("python_path") or "").strip()
            args = build_args(cu)
            custom_env = build_env_lines(cu.get("env_lines")) if cu.get("enable_custom_env") else {}
            try:
                port = int(cu.get("port", 8188))
            except (TypeError, ValueError):
                port = 8188
            self._port = port

        if not comfyui_path:
            return {"ok": False, "error": "未配置 ComfyUI 目录（comfyui_path 为空）"}
        if not python_path or not os.path.isfile(python_path):
            return {"ok": False, "error": f"找不到 python.exe，请确认目录是否存在 Python: {python_path or '(未配置)'}"}
        main_py = Path(comfyui_path) / "main.py"
        if not main_py.is_file():
            return {"ok": False, "error": f"找不到 ComfyUI 主程序，请确认是否选择了正确的目录（缺少 main.py）: {comfyui_path}"}
        # 中文/空格路径校验：ComfyUI 无法在含中文或空格的路径下正常运行
        for _label, _p in (("ComfyUI 目录", comfyui_path), ("Python 路径", python_path)):
            if any('\u4e00' <= ch <= '\u9fff' for ch in _p):
                return {"ok": False, "error": f"{_label}不能包含中文: {_p}（请移到纯英文无空格路径，如 D:\\ComfyUI）"}
            if " " in _p:
                return {"ok": False, "error": f"{_label}不能包含空格: {_p}（请移到无空格路径，如 D:\\ComfyUI）"}

        # 端口占用则强制杀掉占用进程树（保证完全接管）
        if self._port_in_use(port):
            pid = self._pid_on_port(port)
            if pid:
                _log("INFO", f"端口 {port} 被进程 {pid} 占用，强制结束其进程树...")
                self._kill_tree(pid)
                for _ in range(30):  # 最多等 15s 释放
                    if not self._port_in_use(port):
                        break
                    time.sleep(0.5)
            else:
                _log("WARN", f"端口 {port} 被占用但未找到监听 PID，放弃启动")
                return {"ok": False, "error": f"端口 {port} 被占用且无法定位进程"}

        cmd = [python_path, "-s", str(main_py)] + list(args)
        _log("INFO", f"启动 ComfyUI: {' '.join(cmd)}")
        if custom_env:
            _log("INFO", "自定义环境变量: " + ", ".join(f"{k}={v}" for k, v in custom_env.items()))
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"   # 强制子进程输出 UTF-8，避免 GBK 乱码
            env["PYTHONUTF8"] = "1"
            if custom_env:
                env.update(custom_env)          # 自定义环境变量覆盖继承值
            proc = subprocess.Popen(
                cmd,
                cwd=str(comfyui_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except Exception as e:
            _log("ERROR", f"启动 ComfyUI 失败: {e}")
            return {"ok": False, "error": str(e)}

        with self._lock:
            self._proc = proc
            self._started_at = time.time()
            self._exit_code = None
            self._last_action = "start"
        self._reader_thread = threading.Thread(target=self._read_output, args=(proc,), daemon=True)
        self._reader_thread.start()
        _log("INFO", f"ComfyUI 已启动 pid={proc.pid}")
        return {"ok": True, "message": "启动成功", "pid": proc.pid}

    # ── 日志读取（线程 C）──
    def _read_output(self, proc: subprocess.Popen):
        try:
            for line in proc.stdout:
                line = line.rstrip("\n").rstrip("\r")
                if line:
                    _log("COMFY", line, source="comfy")
        except Exception as e:
            _log("WARN", f"日志读取终止: {e}")
        code = proc.wait()
        with self._lock:
            self._exit_code = code
            if self._proc is proc:
                self._proc = None
        _log("INFO", f"ComfyUI 进程退出，退出码={code}")

    # ── 停止 ──
    def stop(self) -> dict:
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            pid = proc.pid
            _log("INFO", f"强制结束 ComfyUI pid={pid} 进程树...")
            self._kill_tree(pid)
            # 回收子进程：否则 restart 时 start() 仍看到 poll() is None，误判「已在运行」
            try:
                proc.wait(timeout=15)
            except Exception:
                pass
        else:
            pid = self._pid_on_port(self._port)
            if pid:
                _log("INFO", f"未记录进程，按端口 {self._port} 结束 pid={pid} 进程树...")
                self._kill_tree(pid)
            else:
                _log("INFO", "未发现运行中的 ComfyUI，无需停止")
        for _ in range(30):
            if not self._port_in_use(self._port):
                break
            time.sleep(0.5)
        with self._lock:
            if self._proc is not None and self._proc.poll() is not None:
                self._proc = None
            self._last_action = "stop"
        _log("INFO", f"ComfyUI 已停止（端口 {self._port} 已释放）")
        return {"ok": True, "message": "已停止"}

    # ── 重启 ──
    def restart(self, cfg: dict = None) -> dict:
        _log("INFO", "重启 ComfyUI（先停后启）...")
        self.stop()
        with self._lock:
            self._last_action = "restart"
        return self.start(cfg or load_config())


_manager = ComfyManager()


# ── 鉴权 ──
def _check_auth(key: str) -> bool:
    """X-Key 鉴权。仅校验密钥本身（FastAPI 层已限制监听 127.0.0.1）。"""
    expect = _AUTH.get("api_key") or load_config().get("api_key", "")
    return bool(key) and bool(expect) and secrets.compare_digest(key, expect)


def _check_confirm(confirm: str) -> bool:
    """stop/restart 额外确认。支持 confirm_key 明文或其 sha256。"""
    ck = _AUTH.get("confirm_key") or load_config().get("confirm_key", "")
    if confirm and ck and confirm == ck:
        return True
    if confirm and ck and hashlib.sha256(ck.encode()).hexdigest() == confirm:
        return True
    return False


# ── HTTP 管理服务（线程 B，FastAPI + uvicorn）──
try:
    from fastapi import FastAPI, Header, HTTPException, Query
    from fastapi.responses import JSONResponse
    import uvicorn
except ImportError:
    raise SystemExit("缺少 fastapi/uvicorn，请先安装: pip install fastapi \"uvicorn[standard]\"")


app = FastAPI(title="ComfyUI Launcher", docs_url=None, redoc_url=None)
_APP_ID = secrets.token_hex(8)      # 本进程唯一标识：用来确认「端口上应答的是我自己」


@app.get("/api/health")
def api_health():
    """轻量健康探针（仅本机监听）。返回 pid，供启动阶段确认绑定的是本进程。

    不校验 X-Key：它只暴露 pid/实例号，不含密钥与路径信息，且服务只绑 127.0.0.1。
    """
    return {"ok": True, "app": "comfyui-launcher", "pid": os.getpid(),
            "instance": _APP_ID, "version": CORE_VERSION}


@app.get("/api/status")
def api_status(x_key: str = Header(default="", alias="X-Key")):
    if not _check_auth(x_key):
        raise HTTPException(403, "认证失败或来源受限")
    st = _manager.status()
    _log("INFO", f"API status 查询 pid={st['pid']} running={st['running']}")
    return {"ok": True, **st}


@app.get("/api/logs")
def api_logs(tail: int = Query(200, ge=1, le=500),
             source: str = Query("launcher"),
             x_key: str = Header(default="", alias="X-Key")):
    if not _check_auth(x_key):
        raise HTTPException(403, "认证失败或来源受限")
    src = (source or "launcher").strip().lower()
    if src not in ("launcher", "comfy"):
        raise HTTPException(400, "source 仅支持 launcher / comfy")
    with _log_lock:
        ring = LOG_RING if src == "launcher" else COMFY_RING
        lines = list(ring)[-tail:]
    path = str(LOG_PATH if src == "launcher" else COMFY_LOG_PATH)
    return {"ok": True, "source": src, "log": lines, "count": len(lines), "file": path}


@app.post("/api/start")
def api_start(x_key: str = Header(default="", alias="X-Key")):
    if not _check_auth(x_key):
        raise HTTPException(403, "认证失败或来源受限")
    r = _manager.start(load_config())
    return JSONResponse(r, status_code=200 if r.get("ok") else 500)


@app.post("/api/stop")
def api_stop(x_key: str = Header(default="", alias="X-Key"),
             x_confirm: str = Header(default="", alias="X-Confirm"),
             body: dict = None):
    if not _check_auth(x_key):
        raise HTTPException(403, "认证失败或来源受限")
    if not _check_confirm(x_confirm) and not (body and body.get("confirm") is True):
        raise HTTPException(403, "缺少确认（X-Confirm）")
    r = _manager.stop()
    return JSONResponse(r, status_code=200 if r.get("ok") else 500)


@app.post("/api/restart")
def api_restart(x_key: str = Header(default="", alias="X-Key"),
                x_confirm: str = Header(default="", alias="X-Confirm"),
                body: dict = None):
    if not _check_auth(x_key):
        raise HTTPException(403, "认证失败或来源受限")
    if not _check_confirm(x_confirm) and not (body and body.get("confirm") is True):
        raise HTTPException(403, "缺少确认（X-Confirm）")
    r = _manager.restart(load_config())
    return JSONResponse(r, status_code=200 if r.get("ok") else 500)


BOUND = {"host": "", "port": 0}   # HTTP 服务实际绑定地址（GUI 展示/端口变更提示用）


def start_http_server(cfg: dict) -> threading.Thread:
    """起 HTTP 管理服务。绑定是否成功以「真的能连上」为准（Windows 上端口处于
    TIME_WAIT 时 bind 会报 10048，旧实现照样打印「已启动」，实际 API 完全不可用）。"""
    host = cfg.get("listen_host", "127.0.0.1")
    port = int(cfg.get("listen_port", 18808))

    def _attempt() -> threading.Thread:
        # log_config=None：uvicorn 跳过日志 dictConfig，避免 windowed 模式
        # （sys.stdout 为 None）下 isatty() 崩溃；uvicorn 日志由启动器 _log 接管
        config = uvicorn.Config(app, host=host, port=port, log_config=None,
                                access_log=False, loop="asyncio")
        server = uvicorn.Server(config)

        def _run():
            try:
                server.run()
            except Exception as e:
                _log("ERROR", f"HTTP 管理服务异常退出: {type(e).__name__}: {e}")

        th = threading.Thread(target=_run, daemon=True, name="http-server")
        th.start()
        return th

    def _api_alive() -> Optional[bool]:
        """确认「本进程」真的绑上了端口。

        返回 True=我们自己；False=端口被另一个启动器实例占着（pid 不同）；
        None=端口没人应答。
        旧实现只要求 /api/status 回 200/403，结果被另一个实例的 403 骗过，
        明明没绑上却打印「HTTP 管理服务已启动」。
        """
        import http.client
        try:
            c = http.client.HTTPConnection(host, port, timeout=2)
            c.request("GET", "/api/health")
            r = c.getresponse()
            body = r.read()
            c.close()
            data = json.loads(body.decode("utf-8", "replace"))
            if r.status == 200 and data.get("app") == "comfyui-launcher":
                # 用实例号比对（比 pid 更严格：同进程里多个实例也区分得开）
                return data.get("instance") == _APP_ID
            return None        # 端口上是别的程序，不是启动器
        except Exception:
            return None

    th = None
    occupied_by_other = False
    for i in range(4):                     # 最多 4 次尝试，总等待约 12s
        th = _attempt()
        for _ in range(12):                # 每次最多等 3s 确认可连接
            alive = _api_alive()
            if alive is True:
                BOUND["host"] = host
                BOUND["port"] = port
                _log("INFO", f"HTTP 管理服务已启动: http://{host}:{port}")
                return th
            if alive is False:
                occupied_by_other = True
                break
            time.sleep(0.25)
        if occupied_by_other:
            break
        _log("WARN", f"管理端口 {port} 第 {i + 1} 次绑定未成功，重试中...")
        time.sleep(1.0)

    BOUND["host"] = ""
    BOUND["port"] = 0
    if occupied_by_other:
        _log("ERROR", f"管理端口 {port} 已被另一个启动器实例占用，本实例的 HTTP 管理服务未启动；"
                      f"关掉多余的启动器实例（或改端口）后重启")
    else:
        _log("ERROR", f"管理端口 {port} 无法绑定（可能被其他程序占用或处于 TIME_WAIT），"
                      f"HTTP 管理服务未启动；请改「管理端口」后重启启动器")
    return th


# ── GUI（主线程）──
def run_gui():
    import tkinter as tk
    from tkinter import ttk, scrolledtext, filedialog, messagebox

    cfg = load_config()

    root = tk.Tk()
    root.title("ComfyUI 启动器")
    root.geometry("900x880")
    root.minsize(720, 560)

    # 状态区
    status_var = tk.StringVar(value="状态: 加载中...")

    def refresh_status():
        st = _manager.status()
        if st["running"]:
            status_var.set(
                f"● 运行中  PID={st['pid']}  启动={datetime.fromtimestamp(st['started_at']).strftime('%H:%M:%S')}"
            )
        else:
            extra = f"  上次退出码={st['exit_code']}" if st["exit_code"] is not None else ""
            status_var.set(f"○ 已停止{extra}")
        root.after(2000, refresh_status)

    # ── 顶栏 ──
    top = tk.Frame(root, padx=10, pady=6)
    top.pack(fill="x")
    tk.Label(top, textvariable=status_var, font=("Microsoft YaHei UI", 11, "bold"),
             fg="#0a7d32").pack(side="left")
    # 内核版本：来自 core.py 的 CORE_VERSION（热更新替换 core.py 后，重启即变）
    tk.Label(top, text=f"内核 v{CORE_VERSION}", font=("Microsoft YaHei UI", 9),
             fg="#555").pack(side="right")
    tk.Label(top, text="版本:", font=("Microsoft YaHei UI", 9), fg="#888").pack(side="right")

    # ── 路径/端口/密钥设置 ──
    frame = tk.LabelFrame(root, text=" 设置（修改后自动保存） ", padx=8, pady=6,
                          font=("Microsoft YaHei UI", 9))
    frame.pack(fill="x", padx=10, pady=4)

    def browse(entry, is_dir):
        start = entry.get() or str(BASE_DIR)
        if is_dir:
            r = filedialog.askdirectory(initialdir=start)
            if r:
                entry.delete(0, "end")
                entry.insert(0, r.replace("/", "\\"))
                entry.config(fg="#000000")
        else:
            r = filedialog.askopenfilename(initialdir=start,
                                           filetypes=[("Python", "*.exe")])
            if r:
                entry.delete(0, "end")
                entry.insert(0, r.replace("/", "\\"))
                entry.config(fg="#000000")

    def setup_placeholder(entry, ph_text):
        """占位提示：值为空时显示灰色提示，聚焦后清空，失焦且为空时恢复提示。"""
        entry.config(fg="#888888")
        if not entry.get().strip():
            entry.delete(0, "end")
            entry.insert(0, ph_text)
            entry.config(fg="#888888")

        def on_focus_in(_):
            if entry.get().strip() == ph_text:
                entry.delete(0, "end")
                entry.config(fg="#000000")

        def on_focus_out(_):
            if not entry.get().strip():
                entry.insert(0, ph_text)
                entry.config(fg="#888888")

        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)

    comfy_var = tk.StringVar(value=cfg["comfyui"]["comfyui_path"])
    py_var = tk.StringVar(value=cfg["comfyui"]["python_path"])
    port_var = tk.StringVar(value=str(cfg["listen_port"]))
    key_var = tk.StringVar(value=cfg["api_key"])
    ckey_var = tk.StringVar(value=cfg["confirm_key"])
    auto_var = tk.BooleanVar(value=bool(cfg.get("auto_start_comfyui", False)))
    auto_update_var = tk.BooleanVar(value=bool((cfg.get("update") or {}).get("auto", False)))
    # 更新代理（留空 = 直连 / 走系统代理）
    _proxy_raw = str((cfg.get("update") or {}).get("proxy") or "")
    _proxy_norm = normalize_proxy(_proxy_raw) or ""
    _proxy_hp = _proxy_norm.split("://", 1)[1] if "://" in _proxy_norm else ""
    _proxy_host, _, _proxy_port = _proxy_hp.rpartition(":") if _proxy_hp else ("", "", "")
    proxy_host_var = tk.StringVar(value=_proxy_host)
    proxy_port_var = tk.StringVar(value=_proxy_port)
    # ── 启动可选项 ──
    fp16_var = tk.BooleanVar(value=bool(cfg["comfyui"].get("use_fp16_accumulation", True)))
    cuda_var = tk.BooleanVar(value=bool(cfg["comfyui"].get("use_cuda_malloc", True)))
    custom_args_on_var = tk.BooleanVar(value=bool(cfg["comfyui"].get("enable_custom_args", False)))
    custom_args_var = tk.StringVar(value=str(cfg["comfyui"].get("custom_args") or ""))
    custom_env_on_var = tk.BooleanVar(value=bool(cfg["comfyui"].get("enable_custom_env", False)))

    def _real_value(s: str) -> str:
        """去掉 placeholder 提示文本，返回真实值。"""
        v = (s or "").strip()
        if v in (PLACEHOLDER_COMFY, PLACEHOLDER_PY):
            return ""
        return v

    def _persist_settings_now():
        """真正落盘（原子写）。由 schedule_persist 防抖调用，窗口关闭前也会刷一次。"""
        cu = cfg["comfyui"]
        cu["comfyui_path"] = _real_value(comfy_var.get())
        cu["python_path"] = _real_value(py_var.get())
        try:
            cfg["listen_port"] = int(port_var.get().strip() or "18808")
        except ValueError:
            cfg["listen_port"] = 18808
        cfg["api_key"] = key_var.get().strip()
        cfg["confirm_key"] = ckey_var.get().strip()
        cfg["auto_start_comfyui"] = bool(auto_var.get())
        cu["use_fp16_accumulation"] = bool(fp16_var.get())
        cu["use_cuda_malloc"] = bool(cuda_var.get())
        cu["enable_custom_args"] = bool(custom_args_on_var.get())
        cu["custom_args"] = custom_args_var.get().strip()
        cu["enable_custom_env"] = bool(custom_env_on_var.get())
        try:
            raw = env_text.get("1.0", "end")
            cu["env_lines"] = [ln.rstrip() for ln in raw.splitlines() if ln.strip()]
        except NameError:
            pass
        u = cfg.setdefault("update", {})
        u["auto"] = bool(auto_update_var.get())
        _ph = proxy_host_var.get().strip()
        _pp = proxy_port_var.get().strip()
        u["proxy"] = f"{_ph}:{_pp}" if (_ph and _pp) else ""
        save_config(cfg)

    _persist_job = {"id": None}

    def schedule_persist(*_args):
        """防抖：停止输入 400ms 后再落盘，避免每敲一个字符写一次配置文件。"""
        if _persist_job["id"] is not None:
            try:
                root.after_cancel(_persist_job["id"])
            except Exception:
                pass
        _persist_job["id"] = root.after(400, _flush)

    def _flush():
        _persist_job["id"] = None
        _persist_settings_now()

    for r in range(2):
        frame.grid_rowconfigure(r, weight=0)
    tk.Label(frame, text="ComfyUI 目录:").grid(row=0, column=0, sticky="w", padx=4, pady=3)
    e1 = tk.Entry(frame, textvariable=comfy_var, width=52)
    e1.grid(row=0, column=1, sticky="we", padx=4)
    tk.Button(frame, text="浏览", command=lambda: browse(e1, True)).grid(row=0, column=2, padx=2)
    setup_placeholder(e1, PLACEHOLDER_COMFY)

    tk.Label(frame, text="Python 路径:").grid(row=1, column=0, sticky="w", padx=4, pady=3)
    e2 = tk.Entry(frame, textvariable=py_var, width=52)
    e2.grid(row=1, column=1, sticky="we", padx=4)
    tk.Button(frame, text="浏览", command=lambda: browse(e2, False)).grid(row=1, column=2, padx=2)
    setup_placeholder(e2, PLACEHOLDER_PY)

    tk.Label(frame, text="管理端口:").grid(row=2, column=0, sticky="w", padx=4, pady=3)
    e3 = tk.Entry(frame, textvariable=port_var, width=10)
    e3.grid(row=2, column=1, sticky="w", padx=4)

    tk.Label(frame, text="启动后自动启动 ComfyUI:").grid(row=5, column=0, sticky="w", padx=4, pady=3)
    tk.Checkbutton(frame, text="自动拉起（否则仅启动管理服务，需手动点「启动」）",
                   variable=auto_var).grid(row=5, column=1, sticky="w", padx=4)

    tk.Label(frame, text="启动自动检测更新:").grid(row=6, column=0, sticky="w", padx=4, pady=3)
    tk.Checkbutton(frame, text="启动时自动对比版本，有更新则下载并重启（否则仅用「检查更新」按钮手动更新）",
                   variable=auto_update_var).grid(row=6, column=1, sticky="w", padx=4)

    # ── 更新代理（可选）──
    tk.Label(frame, text="更新代理:").grid(row=7, column=0, sticky="w", padx=4, pady=3)
    proxy_box = tk.Frame(frame)
    proxy_box.grid(row=7, column=1, sticky="w", padx=4)
    tk.Entry(proxy_box, textvariable=proxy_host_var, width=18).pack(side="left")
    tk.Label(proxy_box, text=" : ").pack(side="left")
    tk.Entry(proxy_box, textvariable=proxy_port_var, width=7).pack(side="left")
    tk.Button(frame, text="测试", command=lambda: do_test_proxy()).grid(row=7, column=2, padx=2)
    tk.Label(frame,
             text="填 http 代理（Clash/V2Ray 的混合端口，如 127.0.0.1:7890）；留空=直连或走系统代理。"
                  "填了之后更新会走代理直连 GitHub 官方地址",
             fg="#888", font=("Microsoft YaHei UI", 8)).grid(row=8, column=1, sticky="w", padx=4)

    tk.Label(frame, text="API 密钥:").grid(row=3, column=0, sticky="w", padx=4, pady=3)
    e4 = tk.Entry(frame, textvariable=key_var, width=52)
    e4.grid(row=3, column=1, sticky="we", padx=4)
    tk.Button(frame, text="生成", command=lambda: key_var.set(secrets.token_hex(16))).grid(row=3, column=2, padx=2)

    tk.Label(frame, text="确认密钥:").grid(row=4, column=0, sticky="w", padx=4, pady=3)
    e5 = tk.Entry(frame, textvariable=ckey_var, width=52)
    e5.grid(row=4, column=1, sticky="we", padx=4)
    tk.Button(frame, text="生成", command=lambda: ckey_var.set(secrets.token_hex(16))).grid(row=4, column=2, padx=2)

    for var in (comfy_var, py_var, port_var, key_var, ckey_var, auto_var, auto_update_var,
                proxy_host_var, proxy_port_var):
        var.trace_add("write", schedule_persist)
    frame.columnconfigure(1, weight=1)

    # ── 启动选项（fp16 / cuda_malloc / 自定义命令 / 自定义环境变量）──
    opt = tk.LabelFrame(root, text=" 启动选项（勾选即生效，下次启动 ComfyUI 时应用） ",
                        padx=8, pady=6, font=("Microsoft YaHei UI", 9))
    opt.pack(fill="x", padx=10, pady=4)
    opt.columnconfigure(1, weight=1)

    tk.Checkbutton(opt, text="开启 fp16_accumulation（追加 --fast fp16_accumulation）",
                   variable=fp16_var).grid(row=0, column=0, columnspan=2, sticky="w", padx=4, pady=2)
    tk.Checkbutton(opt, text="开启 cuda_malloc（勾选追加 --cuda-malloc；取消勾选追加 --disable-cuda-malloc）",
                   variable=cuda_var).grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=2)

    tk.Checkbutton(opt, text="开启自定义命令", variable=custom_args_on_var).grid(
        row=2, column=0, sticky="w", padx=4, pady=2)
    ca_entry = tk.Entry(opt, textvariable=custom_args_var, width=48)
    ca_entry.grid(row=2, column=1, sticky="we", padx=4, pady=2)
    tk.Label(opt, text='例：--port 8188 --lowvram --output-directory "D:\\out dir"',
             fg="#888", font=("Microsoft YaHei UI", 8)).grid(row=3, column=1, sticky="w", padx=4)

    tk.Checkbutton(opt, text="开启自定义环境变量", variable=custom_env_on_var).grid(
        row=4, column=0, sticky="nw", padx=4, pady=2)
    env_box = tk.Frame(opt)
    env_box.grid(row=4, column=1, sticky="we", padx=4, pady=2)
    env_text = tk.Text(env_box, height=3, width=52, font=("Consolas", 9), wrap="none")
    env_text.pack(fill="x")
    for ln in (cfg["comfyui"].get("env_lines") or []):
        env_text.insert("end", str(ln) + "\n")

    tk.Label(opt, text="每行一个 KEY=VALUE（# 开头为注释），会覆盖同名系统环境变量",
             fg="#888", font=("Microsoft YaHei UI", 8)).grid(row=5, column=1, sticky="w", padx=4)

    # 实际命令预览：所见即所得，避免勾了但没生效
    tk.Label(opt, text="实际参数:").grid(row=6, column=0, sticky="nw", padx=4, pady=(4, 2))
    preview_var = tk.StringVar(value="")
    tk.Label(opt, textvariable=preview_var, fg="#0a5", anchor="w", justify="left",
             wraplength=680, font=("Consolas", 8)).grid(row=6, column=1, sticky="we", padx=4, pady=(4, 2))

    def refresh_preview(*_a):
        tmp = {
            "args": cfg["comfyui"].get("args") or [],
            "use_fp16_accumulation": bool(fp16_var.get()),
            "use_cuda_malloc": bool(cuda_var.get()),
            "enable_custom_args": bool(custom_args_on_var.get()),
            "custom_args": custom_args_var.get(),
        }
        preview_var.set(" ".join(build_args(tmp)) or "(无)")

    for var in (fp16_var, cuda_var, custom_args_on_var, custom_args_var, custom_env_on_var):
        var.trace_add("write", schedule_persist)
        var.trace_add("write", refresh_preview)
    env_text.bind("<KeyRelease>", lambda _e: (schedule_persist(), refresh_preview()))

    def _on_toggle_bg(*_a):
        """关闭开关时把对应输入框置灰，减少"勾了但看不出是否生效"的困惑。"""
        ca_entry.config(state="normal" if custom_args_on_var.get() else "disabled")
        env_text.config(state="normal" if custom_env_on_var.get() else "disabled")

    for var in (custom_args_on_var, custom_env_on_var):
        var.trace_add("write", _on_toggle_bg)
    _on_toggle_bg()
    refresh_preview()

    # ── 日志区（双 tab：启动器/API 日志 + ComfyUI 日志）──
    log_frame = tk.LabelFrame(root, text=" 实时日志 ", padx=4, pady=4, font=("Microsoft YaHei UI", 9))
    log_frame.pack(fill="both", expand=True, padx=10, pady=4)

    # 日志工具条（右上角）：清空当前显示 + 环形缓冲 + 日志文件
    log_tools = tk.Frame(log_frame)
    log_tools.pack(fill="x", pady=(0, 2))

    log_nb = ttk.Notebook(log_frame)
    log_nb.pack(fill="both", expand=True)

    def _make_log_page():
        page = tk.Frame(log_nb, bg="#1e1e1e")
        text = scrolledtext.ScrolledText(
            page, height=7, font=("Consolas", 9), state="disabled",
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
        )
        text.pack(fill="both", expand=True)
        return page, text

    launcher_page, launcher_text = _make_log_page()
    comfy_page, comfy_text = _make_log_page()
    log_nb.add(launcher_page, text=" 启动器 / API ")
    log_nb.add(comfy_page, text="  ComfyUI  ")

    def do_clear_logs():
        """清空两个日志页签 + 环形缓冲 + 日志文件（清完留一行提示）。"""
        for t in (launcher_text, comfy_text):
            try:
                t.configure(state="normal")
                t.delete("1.0", "end")
                t.configure(state="disabled")
            except Exception:
                pass
        with _log_lock:
            LOG_RING.clear()
            COMFY_RING.clear()
        # 队列里可能还有没泵出来的旧行，一并丢掉，否则清完又被填回去
        while True:
            try:
                LOG_QUEUE.get_nowait()
            except queue.Empty:
                break
        for p in (LOG_PATH, COMFY_LOG_PATH):
            try:
                with open(p, "w", encoding="utf-8"):
                    pass
            except OSError:
                pass
        _append("INFO", "🧹 日志已清空（显示区 + 内存缓冲 + 日志文件）")

    tk.Button(log_tools, text="🧹 清空日志", command=do_clear_logs,
              font=("Microsoft YaHei UI", 8), padx=8, pady=1).pack(side="right")
    tk.Label(log_tools, text="清空只影响显示区与日志文件，不会动 ComfyUI 进程",
             fg="#888", font=("Microsoft YaHei UI", 8)).pack(side="left", padx=2)

    MAX_LOG_LINES = 4000   # 单个日志面板最多保留行数，防止长时间运行后 Tk 文本控件吃满内存

    def _append_to(text, level, line):
        tag = "INFO" if level in ("INFO", "COMFY") else ("WARN" if level == "WARN" else "ERROR")
        text.configure(state="normal")
        text.insert("end", line + "\n", tag)
        text.tag_config(tag, foreground="#ffffff" if level == "COMFY" else (
            "#ffcc00" if level == "WARN" else "#ff6666"))
        try:
            n = int(text.index("end-1c").split(".")[0])
            if n > MAX_LOG_LINES:
                text.delete("1.0", f"{n - MAX_LOG_LINES + 1}.0")
        except Exception:
            pass
        text.see("end")
        text.configure(state="disabled")

    def _append(level, line, source="launcher"):
        _append_to(launcher_text if source == "launcher" else comfy_text, level, line)

    # ── 线程安全 UI 队列 ──
    # tkinter 只能在主线程操作：worker 线程里直接 root.after(...) 会抛
    # "main thread is not in main loop"（旧代码用 except 吞掉，导致弹窗关不掉、日志不刷新）。
    # 统一改走队列，由主线程定时泵出执行。
    _ui_q = queue.Queue()

    def ui_call(fn, *args):
        _ui_q.put((fn, args))

    def pump_ui():
        while True:
            try:
                fn, args = _ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception as e:
                _log("ERROR", f"UI 回调异常: {type(e).__name__}: {e}")
        root.after(50, pump_ui)

    def pump_logs():
        while True:
            try:
                ts, source, level, line = LOG_QUEUE.get_nowait()
            except queue.Empty:
                break
            if source == "comfy":
                _append_to(comfy_text, level, line)
            else:
                _append_to(launcher_text, level, line)
        root.after(200, pump_logs)

    # ── 按钮区 ──
    btn_frame = tk.Frame(root, padx=10, pady=8)
    # 固定在状态栏下方：日志/选项再长也不会把操作按钮挤出窗口可视区
    btn_frame.pack(fill="x", before=frame)

    def _run_in_thread(fn):
        threading.Thread(target=fn, daemon=True).start()

    def do_start():
        _persist_settings_now()
        cu = load_config().get("comfyui") or {}
        cpath = str(cu.get("comfyui_path") or "").strip()
        ppath = str(cu.get("python_path") or "").strip()

        # 1. 目录完全未设置
        if not cpath and not ppath:
            messagebox.showerror(
                "启动环境未配置",
                "启动环境查找失败：未设置 ComfyUI 目录和 Python 路径。\n\n"
                "请在「设置」里选择正确的 ComfyUI 主程序目录和 python.exe，"
                "完成后点击「启动」。",
            )
            return

        # 2. 中文/空格检查（ComfyUI/Python 不支持含中文或空格的路径）
        for label, p in (("ComfyUI 目录", cpath), ("Python 路径", ppath)):
            if p:
                if any('\u4e00' <= ch <= '\u9fff' for ch in p):
                    messagebox.showerror(
                        f"{label} 包含中文",
                        f"{label}不能包含中文：\n{p}\n\n"
                        "请把 ComfyUI 移到纯英文、无空格的路径（如 D:\\ComfyUI）。",
                    )
                    return
                if " " in p:
                    messagebox.showerror(
                        f"{label} 包含空格",
                        f"{label}不能包含空格：\n{p}\n\n"
                        "请把 ComfyUI 移到无空格的路径（如 D:\\ComfyUI）。",
                    )
                    return

        # 3. ComfyUI 主程序校验
        if not cpath:
            messagebox.showerror(
                "ComfyUI 目录未设置",
                "找不到 ComfyUI 主程序，请确认是否选择了正确的目录。\n\n"
                "请在「设置」里选择 ComfyUI 主程序所在目录（应包含 main.py 的目录）。",
            )
            return
        if not (Path(cpath) / "main.py").is_file():
            messagebox.showerror(
                "ComfyUI 主程序不存在",
                f"找不到 ComfyUI 主程序，请确认是否选择了正确的目录：\n{cpath}\n\n"
                "该目录下应存在 main.py 文件。启动环境查找失败，无法完成启动，"
                "请检查相应目录的正确性。",
            )
            return

        # 4. Python 校验
        if not ppath:
            messagebox.showerror(
                "Python 路径未设置",
                "找不到 python.exe，请确认目录是否存在 Python。\n\n"
                "请在「设置」里选择 python.exe（通常位于 ComfyUI 便携版的 "
                "python_embeded 目录下）。",
            )
            return
        if not os.path.isfile(ppath):
            messagebox.showerror(
                "Python 不存在",
                f"找不到 python.exe，请确认目录是否存在 Python：\n{ppath}\n\n"
                "请检查相应目录的正确性。",
            )
            return

        _run_in_thread(lambda: _do_start())

    def _do_start():
        r = _manager.start(load_config())
        if not r.get("ok"):
            ui_call(messagebox.showerror, "启动失败", r.get("error", "未知错误"))
        else:
            ui_call(_append, "INFO", f"✓ {r.get('message')}")

    def do_stop():
        _persist_settings_now()
        if messagebox.askyesno("确认停止", "强制结束 ComfyUI 进程树？"):
            _run_in_thread(lambda: _do_stop())

    def _do_stop():
        r = _manager.stop()
        ui_call(_append, "INFO", f"✓ {r.get('message')}")

    def do_restart():
        _persist_settings_now()
        if messagebox.askyesno("确认重启", "将强制停止后重新启动 ComfyUI，确定？"):
            _run_in_thread(lambda: _do_restart())

    def _do_restart():
        r = _manager.restart(load_config())
        if not r.get("ok"):
            ui_call(messagebox.showerror, "重启失败", r.get("error", "未知错误"))
        else:
            ui_call(_append, "INFO", f"✓ {r.get('message')}")

    def do_test_proxy():
        """测试代理能否拉到 GitHub 官方清单（不写配置，纯探测）。"""
        host_txt = proxy_host_var.get().strip()
        port_txt = proxy_port_var.get().strip()
        spec = f"{host_txt}:{port_txt}" if (host_txt and port_txt) else ""

        def _worker():
            r = probe_network(spec)
            if r.get("ok"):
                msg = (f"连通正常\n\n代理: {r.get('proxy') or '未使用（直连 / 系统代理）'}\n"
                       f"远端版本: v{r.get('version')}\n耗时: {r.get('ms')} ms")
                ui_call(_append, "INFO", f"✓ 网络测试通过 {r.get('ms')}ms  v{r.get('version')}"
                                  f"（代理: {r.get('proxy') or '直连'}）")
                ui_call(messagebox.showinfo, "网络测试", msg)
            else:
                msg = (f"拉取失败\n\n代理: {r.get('proxy') or '未使用（直连 / 系统代理）'}\n"
                       f"地址: {r.get('url')}\n错误: {r.get('error')}")
                ui_call(_append, "WARN", f"⚠ 网络测试失败: {r.get('error')}")
                ui_call(messagebox.showerror, "网络测试", msg)

        _run_in_thread(_worker)

    # ── 「请稍等」小窗：检测/下载期间显示在最前面，结束自动关闭 ──
    def _show_busy(title_text, hint_text, slow_hint="网络较慢，仍在检测，请稍候…"):
        """弹出置顶小窗（带滚动进度条），返回幂等的 close()。

        Tk 只能在主线程操作，这里全程在主线程创建；网络请求在子线程，结束后
        由 ui_call 交回主线程销毁，避免跨线程碰控件。
        """
        win = tk.Toplevel(root)
        win.title(title_text)
        win.transient(root)
        win.resizable(False, False)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        body = tk.Frame(win, padx=20, pady=16)
        body.pack(fill="both", expand=True)
        label = tk.Label(body, text=hint_text, font=("Microsoft YaHei UI", 10))
        label.pack(anchor="w")
        pb = ttk.Progressbar(body, mode="indeterminate", length=280)
        pb.pack(pady=(12, 0), fill="x")
        try:
            pb.start(12)
        except Exception:
            pass

        win.update_idletasks()
        try:                                    # 居中偏上，别挡住主按钮
            x = root.winfo_rootx() + (root.winfo_width() - win.winfo_width()) // 2
            y = root.winfo_rooty() + (root.winfo_height() - win.winfo_height()) // 3
            win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            pass
        win.lift()
        try:
            win.focus_force()
        except Exception:
            pass

        state = {"closed": False}

        def _slower():
            if not state["closed"] and slow_hint:
                try:
                    label.config(text=slow_hint)
                except Exception:
                    pass

        try:
            root.after(1500, _slower)           # 超过 1.5 秒还没结果就提示慢
        except Exception:
            pass

        def close():
            if state["closed"]:
                return
            state["closed"] = True
            try:
                pb.stop()
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

        win.protocol("WM_DELETE_WINDOW", close)   # 允许用户自己点 X，close 幂等
        return close

    def do_check_update():
        close_busy = _show_busy("检查更新", "正在检测新版本，请稍等…")

        def _to_main(fn, *a):
            try:
                ui_call(fn, *a)
            except Exception:
                pass

        def _worker():
            try:
                st = check_update(verbose=True)
            except Exception as e:
                _to_main(lambda: (close_busy(), _append("ERROR", f"✗ 检查更新异常: {e}")))
                return
            _to_main(_on_checked, st)

        def _on_checked(st):
            close_busy()
            if not st.get("ok"):
                _append("WARN", f"⚠ 检查更新失败: {st.get('reason', '')}")
                messagebox.showwarning("检查更新",
                                       f"检测失败：{st.get('reason', '未知原因')}\n\n"
                                       "可稍后重试；有代理的话在「更新代理」里填上 IP 和端口再试。")
                return
            core_up = bool(st.get("core_has_update"))
            exe_up = bool(st.get("exe_has_update"))
            if not core_up and not exe_up:
                _append("INFO", f"✓ {st.get('reason', '')} (v{st.get('current', '')})")
                messagebox.showinfo("检查更新",
                                    f"当前已是最新版本 v{st.get('current', '')}。")
                return

            parts = []
            if core_up:
                parts.append("· 核心逻辑 core.py（热更新，无需重下 exe）")
            if exe_up:
                size = st.get("exe_size") or 0
                parts.append(f"· 启动器本体 exe（{size / 1048576:.1f} MB，会替换文件并自动重启）"
                             if size else "· 启动器本体 exe（会替换文件并自动重启）")
            msg = (f"发现新版本 v{st.get('latest')}\n当前 v{st.get('current')}"
                   f"\n\n本次更新包含:\n" + "\n".join(parts))
            cl = st.get("changelog")
            if cl:
                msg += f"\n\n更新说明:\n{cl}"
            if not messagebox.askyesno("发现新版本", msg + "\n\n是否立即更新？"):
                return
            # 下载阶段：同样用置顶小窗提示，避免界面像"卡住"
            _apply_update_async(st, core_up, exe_up)

        def _apply_update_async(st, core_up, exe_up):
            close_dl = _show_busy("正在更新", "正在下载并校验更新，请稍等…")

            def _done_exe(script):
                close_dl()
                _append("INFO", "✓ 本体已下载，正在替换并重启…")
                root.after(900, lambda: launch_exe_swap(script))

            def _done_core_only(ver):
                close_dl()
                _append("INFO", f"✓ 更新完成，正在重启… v{ver}")
                root.after(1200, restart_self)

            def _fail_exe(err):
                close_dl()
                _append("WARN", f"⚠ 本体更新失败: {err}")
                messagebox.showerror("本体更新失败",
                                     f"{err}\n\n核心逻辑的更新已生效，重启后仍是旧版 exe。")
                root.after(1500, restart_self)

            def _fail_core(err):
                close_dl()
                _append("ERROR", f"✗ 核心更新失败: {err}")
                messagebox.showerror("核心更新失败", err)

            def _do():
                try:
                    if core_up:
                        r = apply_update()
                        if not r.get("ok"):
                            _to_main(_fail_core, r.get("error", "未知错误"))
                            return
                        _to_main(_append, "INFO", f"✓ core.py 已更新到 v{st.get('latest')}")

                    if exe_up:
                        r2 = apply_exe_update()
                        if not r2.get("ok"):
                            _to_main(_fail_exe, r2.get("error", "未知错误"))
                            return
                        _to_main(_done_exe, r2["script"])
                        return

                    _to_main(_done_core_only, st.get("latest"))
                except Exception as e:
                    _to_main(lambda: (close_dl(), _append("ERROR", f"✗ 更新异常: {e}")))

            _run_in_thread(_do)

        _run_in_thread(_worker)

    tk.Button(btn_frame, text="🚀 启动", command=do_start, bg="#0a7d32", fg="white",
              font=("Microsoft YaHei UI", 11, "bold"), padx=18, pady=4).pack(side="left", padx=6)
    tk.Button(btn_frame, text="⏹ 停止", command=do_stop, bg="#c0392b", fg="white",
              font=("Microsoft YaHei UI", 11, "bold"), padx=18, pady=4).pack(side="left", padx=6)
    tk.Button(btn_frame, text="🔄 重启", command=do_restart, bg="#e67e22", fg="white",
              font=("Microsoft YaHei UI", 11, "bold"), padx=18, pady=4).pack(side="left", padx=6)
    tk.Button(btn_frame, text="📥 检查更新", command=do_check_update, bg="#6c5ce7", fg="white",
              font=("Microsoft YaHei UI", 11, "bold"), padx=18, pady=4).pack(side="left", padx=6)

    hint = tk.Label(root, text="", fg="#888", font=("Microsoft YaHei UI", 9))
    hint.pack(fill="x", padx=10, pady=(0, 6), before=frame)

    def refresh_hint():
        """显示真实生效的管理地址 + 内核/本体标识；端口被改过则提示需重启才生效。"""
        bound = BOUND["port"] or int(cfg["listen_port"])
        _exe = exe_path()
        if _exe:
            _sha = exe_sha256()
            body = f"本体 {_exe.name} ({_sha[:8]})" if _sha else f"本体 {_exe.name}"
        else:
            body = "本体 源码模式"
        txt = (f"管理地址: http://{BOUND['host'] or cfg['listen_host']}:{bound}"
               f"   |   内核 v{CORE_VERSION}（core.py）   |   {body}")
        try:
            want = int(port_var.get().strip() or "18808")
        except ValueError:
            want = bound
        if want != bound:
            txt += f"   ⚠ 端口已改为 {want}，重启启动器后生效"
        hint.config(text=txt)
        root.after(1000, refresh_hint)

    def _on_close():
        if _persist_job["id"] is not None:
            try:
                root.after_cancel(_persist_job["id"])
            except Exception:
                pass
        _persist_settings_now()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)

    root.after(50, pump_ui)
    root.after(200, pump_logs)
    root.after(500, refresh_status)
    root.after(300, refresh_hint)
    _log("INFO", f"GUI 就绪，管理端口 {cfg['listen_port']}")
    root.mainloop()


# ══════════════════════════════════════════════════════════════
# 更新机制（由壳 exe 调用；core.py 本身可被热替换）
# ══════════════════════════════════════════════════════════════

CORE_VERSION = "1.0.11"
# 更新源：GitHub 直连 + 加速镜像列表 + 仓库地址。version.json 与 core.py 放仓库根目录。
# 逻辑文件（version.json / core.py）：prefix + /<repo>/<branch>/<file>
# Release 附件（exe）：prefix + /https://github.com/<repo>/releases/download/<tag>/<name>
UPDATE_CONFIG = {
    "mirrors": [
        "https://raw.githubusercontent.com",              # 直连（有代理的用户最快）
        "https://gh-proxy.com/https://raw.githubusercontent.com",
        "https://ghproxy.net/https://raw.githubusercontent.com",
    ],
    "repo": "vrc-man/comfyui-launcher",
    "branch": "main",
    "version_file": "version.json",
    "core_file": "core.py",
    "timeout": 15,
}
CORE_PATH = BASE_DIR / "core.py"

# exe 自更新：只替换「启动器本体」，与 core.py 热更新相互独立
_EXE_PATH_OVERRIDE = None          # 仅测试用：强制指定 exe 路径
_EXE_HASH = {"path": "", "sha": ""}


def _load_update_config(cfg: dict) -> dict:
    """合并用户配置覆盖：config.json 里可写 update.mirrors/repo/branch/proxy 覆盖默认。"""
    u = cfg.get("update") or {}
    merged = json.loads(json.dumps(UPDATE_CONFIG))
    if u.get("mirrors"):
        merged["mirrors"] = u["mirrors"]
    for k in ("repo", "branch", "version_file", "core_file", "timeout"):
        if u.get(k) is not None:
            merged[k] = u[k]
    merged["proxy"] = normalize_proxy(u.get("proxy")) or ""
    return merged


def normalize_proxy(value) -> str:
    """把用户填的代理整理成 http://host:port。

    支持：`127.0.0.1:7890`、`http://127.0.0.1:7890`、`https://host:port`、`host`（默认 8080 不管）。
    socks5 需要 PySocks，这里不静默失败，返回空串由调用方给出提示。
    """
    s = str(value or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low.startswith(("socks5://", "socks4://", "socks://")):
        return ""                      # urllib 不原生支持 socks，别假装能用
    if "://" not in s:
        s = "http://" + s
    scheme, _, rest = s.partition("://")
    if scheme.lower() not in ("http", "https"):
        return ""
    rest = rest.strip("/")
    if not rest or ":" not in rest:
        return ""
    host, _, port = rest.rpartition(":")
    if not host or not port.isdigit() or not (0 < int(port) < 65536):
        return ""
    return f"{scheme.lower()}://{host}:{port}"


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _http_get_bytes(url: str, timeout: int, proxy: str = "") -> Optional[bytes]:
    """HTTP(S) 取字节。proxy 为空时走系统默认（与原行为一致）；填了就强制走该代理。"""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "comfyui-launcher/updater"})
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        with opener.open(req, timeout=timeout) as r:
            return r.read()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_version_info(ucfg: dict, newer_than: str = "") -> Optional[dict]:
    """拉 version.json：所有源并行问一遍，取版本号最高的那份。

    为什么并行：串行时一个慢镜像（实测 gh-proxy 有一次 6.9s）会把「检查更新」
    拖到 8 秒以上，用户只看到「请稍等」在转。并行后总耗时 ≈ 最慢的那个，且
    一旦发现某源版本已经比本地新，就直接返回，不必等慢源。

    为什么还要问所有源：raw.githubusercontent.com 有 CDN 缓存，刚发版时可能
    仍返回旧清单；只信第一个源会误判「已是最新」。
    """
    mirrors = list(ucfg.get("mirrors") or [])
    if not mirrors:
        return None
    timeout = min(5, int(ucfg.get("timeout", 15)))
    proxy = ucfg.get("proxy", "")
    base = f"{ucfg['repo']}/{ucfg['branch']}/{ucfg['version_file']}"
    local_t = _ver_tuple(newer_than) if newer_than else ()

    got, lock = [], threading.Lock()

    def _one(m):
        try:
            data = _http_get_bytes(f"{m}/{base}", timeout, proxy)
            info = json.loads(data.decode("utf-8-sig"))
            if isinstance(info, dict):
                with lock:
                    got.append(info)
        except Exception:
            pass

    threads = [threading.Thread(target=_one, args=(m,), daemon=True) for m in mirrors]
    for t in threads:
        t.start()

    deadline = time.time() + timeout + 1.0
    while time.time() < deadline:
        with lock:
            snapshot = list(got)
        best, best_t = None, ()
        for info in snapshot:
            vt = _ver_tuple(str(info.get("version", "")))
            if best is None or vt > best_t:
                best, best_t = info, vt
        if local_t and best_t and best_t > local_t:
            return best                      # 已确认有更新，慢源不等了
        # 两个源报了同一版本 → 基本可排除「某源缓存旧清单」，直接采用
        if len(snapshot) >= 2:
            vers = [str(i.get("version", "")) for i in snapshot]
            if max(vers.count(v) for v in set(vers)) >= 2:
                return best
        if len(snapshot) == len(mirrors):
            return best
        time.sleep(0.05)

    with lock:
        snapshot = list(got)
    best, best_t = None, ()
    for info in snapshot:
        vt = _ver_tuple(str(info.get("version", "")))
        if best is None or vt > best_t:
            best, best_t = info, vt
    return best


def _fetch_all_version_info(ucfg: dict) -> list:
    """把所有源的 version.json 都取回来（按版本号从新到旧），用于「清单↔文件」配对校验。"""
    out, seen = [], set()
    for mirror in ucfg["mirrors"]:
        url = f"{mirror}/{ucfg['repo']}/{ucfg['branch']}/{ucfg['version_file']}"
        try:
            info = json.loads(_http_get_bytes(url, min(8, int(ucfg.get("timeout", 15))), ucfg.get("proxy", "")).decode("utf-8-sig"))
        except Exception:
            continue
        if not isinstance(info, dict):
            continue
        key = (str(info.get("version", "")), str(info.get("core_sha256", "")).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(info)
    out.sort(key=lambda i: _ver_tuple(str(i.get("version", ""))), reverse=True)
    return out


def _download_core_blobs(ucfg: dict) -> list:
    """从所有源下载 core.py，按内容哈希去重（同一版本可能只在一部分源上已刷新）。"""
    blobs, seen = [], set()
    for mirror in ucfg["mirrors"]:
        url = f"{mirror}/{ucfg['repo']}/{ucfg['branch']}/{ucfg['core_file']}"
        try:
            data = _http_get_bytes(url, int(ucfg.get("timeout", 15)), ucfg.get("proxy", ""))
        except Exception:
            continue
        h = hashlib.sha256(data).hexdigest()
        if h not in seen:
            seen.add(h)
            blobs.append(data)
    return blobs


def _download_core(ucfg: dict) -> Optional[bytes]:
    for mirror in ucfg["mirrors"]:
        url = f"{mirror}/{ucfg['repo']}/{ucfg['branch']}/{ucfg['core_file']}"
        try:
            return _http_get_bytes(url, int(ucfg.get("timeout", 15)), ucfg.get("proxy", ""))
        except Exception:
            continue
    return None


def _ver_tuple(v: str) -> tuple:
    """把 "1.2.3" 之类版本号变成可比较元组；无法解析的部分忽略。"""
    parts = []
    for seg in str(v or "").strip().lstrip("vV").split("."):
        num = ""
        for ch in seg:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


# ── 启动器本体（exe）自更新 ──
def exe_path() -> Optional[Path]:
    """当前运行的启动器本体路径；源码模式（非 frozen）返回 None。"""
    if _EXE_PATH_OVERRIDE:
        return Path(_EXE_PATH_OVERRIDE)
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return None


def exe_sha256() -> str:
    """本地 exe 的 sha256（带缓存，18MB 不必每次重算）。"""
    p = exe_path()
    if not p:
        return ""
    key = str(p)
    if _EXE_HASH["path"] == key and _EXE_HASH["sha"]:
        return _EXE_HASH["sha"]
    s = _sha256_file(p)
    _EXE_HASH.update(path=key, sha=s)
    return s


RAW_BASE = "https://raw.githubusercontent.com"


def _exe_download_urls(info: dict, ucfg: dict) -> list:
    """exe 下载地址候选（直连优先，再套镜像），支持两种托管方式：

    1) Release 附件  https://github.com/<repo>/releases/download/<tag>/x.exe
       -> 镜像前缀式：{mirror}/https://github.com/...
    2) 仓库内文件    https://raw.githubusercontent.com/<repo>/<branch>/x.exe
       -> 镜像基址替换：https://gh-proxy.com/https://raw.githubusercontent.com/...
          （直接加前缀会拼出 .../https://raw.githubusercontent.com/https://... 的坏链接）
    """
    exe = info.get("exe") or {}
    if exe.get("urls"):
        raw_list = [str(u).strip() for u in exe["urls"]]
    elif exe.get("url"):
        raw_list = [str(exe["url"]).strip()]
    else:
        raw_list = []

    urls = []
    for u in raw_list:
        if not u:
            continue
        urls.append(u)
        if u.startswith(RAW_BASE + "/"):
            tail = u[len(RAW_BASE):]                    # "/<repo>/<branch>/<file>"
            for m in ucfg.get("mirrors", []):
                base = m.rstrip("/")
                if base == RAW_BASE:
                    continue
                urls.append(base + tail)
        elif "/releases/download/" in u:
            for m in ucfg.get("mirrors", []):
                # 镜像配置形如 https://gh-proxy.com/https://raw.githubusercontent.com，
                # 取前半段当代理根；纯直连基址（就是 RAW_BASE 本身）没有代理能力，跳过。
                proxy_root = m.split(RAW_BASE)[0].rstrip("/")
                if not proxy_root:
                    continue
                urls.append(f"{proxy_root}/{u}")
    # 去重保序
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _download_exe(info: dict, ucfg: dict) -> Optional[bytes]:
    for url in _exe_download_urls(info, ucfg):
        try:
            return _http_get_bytes(url, max(60, int(ucfg.get("timeout", 15))), ucfg.get("proxy", ""))
        except Exception:
            continue
    return None


def probe_network(proxy: str = "") -> dict:
    """测试更新源连通性（走 GitHub 官方 raw 地址），给 GUI「测试」按钮用。

    proxy 传 None 表示用配置里的值；传 "" 表示强制直连/系统代理；传 host:port 表示测试该代理。
    """
    ucfg = _load_update_config(load_config())
    if proxy is None:
        p = ucfg.get("proxy", "")
    else:
        raw = str(proxy or "").strip()
        p = normalize_proxy(raw) if raw else ""
        if raw and not p:
            return {"ok": False, "error": "代理格式不对，应形如 127.0.0.1:7890", "proxy": ""}
    url = f"{RAW_BASE}/{ucfg['repo']}/{ucfg['branch']}/{ucfg['version_file']}"
    t0 = time.time()
    try:
        data = _http_get_bytes(url, 8, p)
        info = json.loads(data.decode("utf-8-sig"))
        return {"ok": True, "ms": int((time.time() - t0) * 1000),
                "version": str(info.get("version", "")), "proxy": p, "url": url}
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - t0) * 1000), "proxy": p, "url": url,
                "error": f"{type(e).__name__}: {e}"}


def check_update(verbose: bool = False) -> dict:
    """检查更新。

    两条独立通道：
      - core：本地 core.py sha256 vs 远端 version.json 的 core_sha256（热替换，无需重启 exe）
      - exe ：本地 exe sha256 vs 远端 version.json 的 exe.sha256（需要替换文件 + 重启）

    返回 {ok, has_update(任一通道有更新), core_has_update, exe_has_update, current, latest,
          changelog, exe_url, exe_size, exe_sha256, reason}
    """
    ucfg = _load_update_config(load_config())
    info = _fetch_version_info(ucfg, newer_than=CORE_VERSION)
    if not info:
        return {"ok": False, "has_update": False, "core_has_update": False,
                "exe_has_update": False, "reason": "无法获取版本信息（网络或镜像失效）"}
    remote_ver = str(info.get("version", "")).strip()
    remote_ver_t = _ver_tuple(remote_ver)
    local_ver_t = _ver_tuple(CORE_VERSION)
    downgrade = remote_ver_t < local_ver_t

    # core 通道
    local_hash = _sha256_file(CORE_PATH).lower()
    remote_hash = str(info.get("core_sha256", "")).strip().lower()
    core_has = bool(remote_hash) and local_hash != remote_hash and not downgrade

    # exe 通道
    exe_info = info.get("exe") or {}
    exe_remote = str(exe_info.get("sha256", "")).strip().lower()
    exe_local = exe_sha256().lower()
    exe_has = bool(exe_remote) and bool(exe_local) and exe_remote != exe_local and not downgrade

    if downgrade and (remote_hash or exe_remote):
        reason = f"远端版本 v{remote_ver} 比本地 v{CORE_VERSION} 旧，已跳过更新"
    elif core_has or exe_has:
        reason = ""
    else:
        reason = "当前已是最新版本"
    return {
        "ok": True,
        "has_update": bool(core_has or exe_has),
        "core_has_update": core_has,
        "exe_has_update": exe_has,
        "current": CORE_VERSION,
        "latest": remote_ver,
        "changelog": str(info.get("changelog", "")),
        "exe_url": (_exe_download_urls(info, ucfg) or [""])[0],
        "exe_size": int(exe_info.get("size") or 0),
        "exe_sha256": exe_remote,
        "reason": reason,
    }


def apply_update() -> dict:
    """下载新 core.py → 与远端清单配对校验 → 备份 → 原子替换。

    关键点：所有源的 version.json 和 core.py 都取一遍，只要能找到「某份清单的哈希
    等于某份下载内容」的配对就采用。这样即使 raw CDN 上 version.json 还是旧缓存、
    core.py 已刷新（或反过来），也不会误报「校验失败」——任一源自洽即可完成更新。
    """
    ucfg = _load_update_config(load_config())
    infos = _fetch_all_version_info(ucfg)
    if not infos:
        return {"ok": False, "error": "无法获取版本信息（网络或镜像失效）"}
    blobs = _download_core_blobs(ucfg)
    if not blobs:
        return {"ok": False, "error": "下载 core.py 失败"}

    data, picked_ver = None, ""
    for blob in blobs:
        h = hashlib.sha256(blob).hexdigest().lower()
        for info in infos:                      # infos 已按版本从新到旧
            if str(info.get("core_sha256", "")).strip().lower() == h:
                data, picked_ver = blob, str(info.get("version", ""))
                break
        if data is not None:
            break
    if data is None:
        return {"ok": False,
                "error": "远端 version.json 与 core.py 不同步（CDN 缓存未刷新），请稍后重试"}

    bak = CORE_PATH.with_suffix(".py.bak")
    tmp = CORE_PATH.with_suffix(".py.tmp")
    try:
        # 顺序很重要：先落盘新文件并校验大小，再备份旧文件，最后原子替换。
        # 旧实现先 os.replace(core.py -> bak) 再写 tmp，中途崩溃会留下「没有 core.py」的死局。
        tmp.write_bytes(data)
        if tmp.stat().st_size != len(data):
            return {"ok": False, "error": "写入不完整，已放弃更新"}
        if CORE_PATH.is_file():
            os.replace(CORE_PATH, bak)          # 覆盖式刷新备份（回滚点始终是本机上一版）
        os.replace(tmp, CORE_PATH)              # 原子替换
        return {"ok": True, "file": str(CORE_PATH), "backup": str(bak), "version": picked_ver}
    except Exception as e:
        # 回滚：替换失败则恢复备份
        try:
            if not CORE_PATH.is_file() and bak.is_file():
                os.replace(bak, CORE_PATH)
            if tmp.is_file():
                tmp.unlink()
        except Exception:
            pass
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def cleanup_update_leftovers() -> None:
    """启动时清掉上个版本更新残留（.old / 未用的 .new / 替换脚本）。

    正在运行的 exe 删不掉，所以「自己改名成 .old」的那份必须等下一次启动才能清。
    """
    exe = exe_path()
    if not exe:
        return
    for path in (exe.with_name(exe.name + ".old"),
                 exe.with_name(exe.name + ".new"),
                 exe.with_name("_comfy_launcher_update.cmd")):
        try:
            if path.is_file():
                path.unlink()
                _log("INFO", f"已清理更新残留: {path.name}")
        except OSError:
            pass


def _swap_script(exe: Path, new: Path) -> Path:
    """生成「等本进程退出 → 换掉 exe → 启动新版」的批处理。

    Windows 上运行中的 exe 不能直接覆盖，但可以改名：脚本先 rename 自己为 .old，
    再把 .new 改成正式名字，然后拉起新进程，最后清掉 .old 与脚本自身。
    """
    old = exe.with_name(exe.name + ".old")
    script = exe.with_name("_comfy_launcher_update.cmd")
    # 注意两点：
    #  1) 不用 chcp/timeout —— 无控制台的分离进程里 timeout 会直接报"输入重定向不受支持"，
    #     改用 ping 做延时；也不切代码页，脚本按系统 ANSI 写盘，中文路径才不会乱码。
    #  2) 脚本与 exe 同目录，路径用绝对形式（%EXE% 由生成时填入）。
    body = f"""@echo off
setlocal
set "EXE={exe}"
set "NEW={new}"
set "OLD={old}"
set /a TRIES=0
if exist "%OLD%" del /f /q "%OLD%" >nul 2>&1
if not exist "%NEW%" goto nonew
:wait
rem 不停尝试改名：改名成功 = 旧进程已经放开 exe（比 tasklist 判断更可靠，
rem 而且 tasklist 在无控制台的分离进程里会直接挂死）
move /y "%EXE%" "%OLD%" >nul 2>&1
if not errorlevel 1 goto moved
set /a TRIES+=1
if %TRIES% GEQ 30 goto failed
ping -n 2 127.0.0.1 >nul 2>&1
goto wait
:moved
move /y "%NEW%" "%EXE%" >nul 2>&1
if errorlevel 1 goto rollback
goto launch
:rollback
rem 新文件放不进去：把旧的换回来，绝不让用户既没新版又没启动器
move /y "%OLD%" "%EXE%" >nul 2>&1
goto launch
:launch
if exist "%EXE%" start "" "%EXE%"
ping -n 3 127.0.0.1 >nul 2>&1
if exist "%OLD%" del /f /q "%OLD%" >nul 2>&1
start "" /b cmd /c del /f /q "%~f0" >nul 2>&1
exit /b 0
:failed
if exist "%OLD%" if not exist "%EXE%" move /y "%OLD%" "%EXE%" >nul 2>&1
if exist "%EXE%" start "" "%EXE%"
start "" /b cmd /c del /f /q "%~f0" >nul 2>&1
exit /b 1
:nonew
if exist "%EXE%" start "" "%EXE%"
start "" /b cmd /c del /f /q "%~f0" >nul 2>&1
exit /b 1
"""
    for enc in ("mbcs", "gbk", "utf-8"):
        try:
            script.write_text(body, encoding=enc, errors="replace", newline="\r\n")
            break
        except (LookupError, UnicodeEncodeError):
            continue
    return script


def stage_exe_update(exe_sha: str = "", info: dict = None) -> dict:
    """下载新 exe 到 <name>.new 并校验，返回 {ok, new, script, version}。

    这一步只落盘，不退出进程；真正替换在 launch_exe_swap() 里。
    """
    exe = exe_path()
    if not exe or not exe.is_file():
        return {"ok": False, "error": "当前是源码模式运行，没有可替换的 exe"}
    ucfg = _load_update_config(load_config())
    infos = [info] if info else _fetch_all_version_info(ucfg)
    if not infos:
        return {"ok": False, "error": "无法获取版本信息"}
    if exe_sha:                       # 调用方指定了哈希：只认这一个
        pairs = [(str(exe_sha).strip().lower(), u)
                 for u in _exe_download_urls({"exe": (infos[0].get("exe") or {})}, ucfg)]
        if not pairs:
            pairs = [(str(exe_sha).strip().lower(), (infos[0].get("exe") or {}).get("url", ""))]
    else:
        # 多份清单 × 多个源：把 (期望哈希, 地址) 全部铺开，配对成功即用。
        # 注意按 (哈希, 地址) 去重而不是只按地址去重：不同清单可能指向同一个地址，
        # 但期望哈希不同（CDN 缓存新旧不一致），只按地址去重会把能配对的那份丢掉。
        pairs, seen_pair = [], set()
        for inf in infos:
            exe_info = inf.get("exe") or {}
            sha = str(exe_info.get("sha256", "")).strip().lower()
            if not sha:
                continue
            for u in _exe_download_urls({"exe": exe_info}, ucfg):
                key = (sha, u)
                if key in seen_pair:
                    continue
                seen_pair.add(key)
                pairs.append((sha, u))
    if not pairs:
        return {"ok": False, "error": "远端 version.json 没有提供 exe 哈希，已放弃更新"}

    data, picked_ver = None, ""
    blobs_by_url = {}                       # 同一地址只下一次，但和所有清单的哈希都比对
    for sha, url in pairs:
        if url not in blobs_by_url:
            try:
                blobs_by_url[url] = _http_get_bytes(url, max(60, int(ucfg.get("timeout", 15))), ucfg.get("proxy", ""))
            except Exception:
                blobs_by_url[url] = None
        blob = blobs_by_url[url]
        if not blob:
            continue
        if hashlib.sha256(blob).hexdigest().lower() == sha:
            data = blob
            for inf in infos:
                if str((inf.get("exe") or {}).get("sha256", "")).strip().lower() == sha:
                    picked_ver = str(inf.get("version", ""))
                    break
            break
    if data is None:
        return {"ok": False,
                "error": "下载新 exe 失败，或远端清单与文件不同步（CDN 缓存未刷新），请稍后重试"}
    new = exe.with_name(exe.name + ".new")
    try:
        new.write_bytes(data)
        if new.stat().st_size != len(data):
            new.unlink(missing_ok=True)
            return {"ok": False, "error": "新 exe 写入不完整"}
        script = _swap_script(exe, new)
        return {"ok": True, "new": str(new), "script": str(script),
                "version": picked_ver, "size": len(data)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def launch_exe_swap(script: str) -> None:
    """脱离当前进程启动替换脚本，然后立刻退出（脚本要等 PID 消失才能改名）。"""
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        ["cmd.exe", "/c", str(script)],
        cwd=str(Path(script).parent),
        # 显式给 stdio：不继承即将关闭的父进程句柄
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    _log("INFO", "已交给后台脚本替换 exe，当前进程退出...")
    os._exit(0)


def apply_exe_update() -> dict:
    """下载 + 校验 + 生成替换脚本（不退出）。调用方随后执行 launch_exe_swap(script)。"""
    r = stage_exe_update()
    if r.get("ok"):
        _log("INFO", f"启动器本体已下载完成（v{r.get('version')}，{r.get('size')} 字节），待替换")
    else:
        _log("ERROR", f"启动器本体更新失败: {r.get('error')}")
    return r


def restart_self() -> None:
    """重启启动器进程（exe 或源码模式均适用）。"""
    exe = sys.executable
    if getattr(sys, "frozen", False):
        cmd = [exe]
    else:
        cmd = [exe, str(BASE_DIR / "comfy_launcher.py")]
    try:
        subprocess.Popen(cmd, cwd=str(BASE_DIR))
        _log("INFO", "已启动新实例，当前进程退出...")
        os._exit(0)
    except Exception as e:
        _log("ERROR", f"重启失败: {e}")


def run_startup_update() -> bool:
    """启动时自动更新（由壳调用）：core 热更新 + exe 本体更新。

    返回 True 表示「已触发更新且进程即将退出」，调用方不应继续往下走。
    """
    st = check_update()
    if not st.get("ok"):
        _log("INFO", f"启动自动更新：{st.get('reason', '检查失败')}")
        return False
    if not st.get("has_update"):
        return False
    _log("INFO", f"发现新版本 v{st.get('latest')}（core={st.get('core_has_update')}, "
                 f"exe={st.get('exe_has_update')}）")
    core_done = False
    if st.get("core_has_update"):
        r = apply_update()
        core_done = bool(r.get("ok"))
        if not core_done:
            _log("WARN", f"core.py 更新失败: {r.get('error')}")
    if st.get("exe_has_update"):
        r2 = apply_exe_update()
        if r2.get("ok"):
            launch_exe_swap(r2["script"])     # 内部 os._exit(0)，正常不会返回
            return False                       # 万一没退出，也别再重复重启
        _log("WARN", f"exe 更新失败，仅应用 core.py 更新: {r2.get('error')}")
    if core_done:
        restart_self()                        # 不返回
    return False

# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════

def main():
    # windowed 打包（--windowed）下 sys.stdout/stderr 为 None，重定向到 devnull
    # 防止第三方库隐式访问 .isatty()/.write() 崩溃
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
    cleanup_update_leftovers()      # 清掉上一轮 exe 自更新的残留文件
    cfg = load_config()
    start_http_server(cfg)
    _log("INFO", f"启动器版本 {CORE_VERSION}，PID={os.getpid()}")
    # 可选：启动后自动拉起 ComfyUI（auto_start_comfyui=true 时）
    if cfg.get("auto_start_comfyui"):
        _log("INFO", "auto_start_comfyui=true，启动后自动拉起 ComfyUI...")
        def _auto_start():
            try:
                r = _manager.start(load_config())
                if not r.get("ok"):
                    _log("WARN", f"自动启动 ComfyUI 失败: {r.get('error', '未知错误')}")
            except Exception as e:
                _log("ERROR", f"自动启动 ComfyUI 异常: {type(e).__name__}: {e}")
        threading.Thread(target=_auto_start, daemon=True).start()
    run_gui()


if __name__ == "__main__":
    main()