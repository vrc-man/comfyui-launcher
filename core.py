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
        "args": [
            "--windows-standalone-build",
            "--disable-auto-launch",
            "--preview-method", "auto",
            "--fast", "fp16_accumulation",
            "--cuda-malloc",
        ],
    },
}


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


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        if CONFIG_PATH.is_file():
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k in cfg:
                if k in saved:
                    cfg[k] = saved[k]
            cu = cfg["comfyui"]
            su = saved.get("comfyui", {})
            for k in cu:
                if k in su:
                    cu[k] = su[k]
    except Exception as e:
        _log("WARN", f"配置读取失败，使用默认: {e}")
    # 密钥兜底：首次运行自动生成
    changed = False
    if not cfg.get("api_key"):
        cfg["api_key"] = secrets.token_hex(16)
        changed = True
    if not cfg.get("confirm_key"):
        cfg["confirm_key"] = secrets.token_hex(16)
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg: dict):
    with _config_lock:
        try:
            CONFIG_PATH.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
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
            args = cu.get("args") or []
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
        try:
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
    cfg = load_config()
    return bool(key) and secrets.compare_digest(key, cfg.get("api_key", ""))


def _check_confirm(confirm: str) -> bool:
    """stop/restart 额外确认。支持 confirm_key 明文或其 sha256。"""
    cfg = load_config()
    ck = cfg.get("confirm_key", "")
    if confirm and confirm == ck:
        return True
    if confirm and hashlib.sha256(ck.encode()).hexdigest() == confirm:
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


def start_http_server(cfg: dict) -> threading.Thread:
    host = cfg.get("listen_host", "127.0.0.1")
    port = int(cfg.get("listen_port", 18808))
    # log_config=None：uvicorn 跳过日志 dictConfig，避免 windowed 模式
    # （sys.stdout 为 None）下 isatty() 崩溃；uvicorn 日志由启动器 _log 接管
    config = uvicorn.Config(app, host=host, port=port, log_config=None,
                            access_log=False, loop="asyncio")
    server = uvicorn.Server(config)

    def _run():
        try:
            server.run()
        except Exception as e:
            _log("ERROR", f"HTTP 管理服务异常退出: {e}")

    t = threading.Thread(target=_run, daemon=True, name="http-server")
    t.start()
    _log("INFO", f"HTTP 管理服务已启动: http://{host}:{port}")
    return t


# ── GUI（主线程）──
def run_gui():
    import tkinter as tk
    from tkinter import ttk, scrolledtext, filedialog, messagebox

    cfg = load_config()

    root = tk.Tk()
    root.title("ComfyUI 启动器")
    root.geometry("780x640")
    root.minsize(660, 520)

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
    auto_update_var = tk.BooleanVar(value=bool((cfg.get("update") or {}).get("auto", True)))

    def _real_value(s: str) -> str:
        """去掉 placeholder 提示文本，返回真实值。"""
        v = (s or "").strip()
        if v in (PLACEHOLDER_COMFY, PLACEHOLDER_PY):
            return ""
        return v

    def persist_settings():
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
        u = cfg.setdefault("update", {})
        u["auto"] = bool(auto_update_var.get())
        save_config(cfg)

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

    tk.Label(frame, text="API 密钥:").grid(row=3, column=0, sticky="w", padx=4, pady=3)
    e4 = tk.Entry(frame, textvariable=key_var, width=52)
    e4.grid(row=3, column=1, sticky="we", padx=4)
    tk.Button(frame, text="生成", command=lambda: key_var.set(secrets.token_hex(16))).grid(row=3, column=2, padx=2)

    tk.Label(frame, text="确认密钥:").grid(row=4, column=0, sticky="w", padx=4, pady=3)
    e5 = tk.Entry(frame, textvariable=ckey_var, width=52)
    e5.grid(row=4, column=1, sticky="we", padx=4)
    tk.Button(frame, text="生成", command=lambda: ckey_var.set(secrets.token_hex(16))).grid(row=4, column=2, padx=2)

    for var in (comfy_var, py_var, port_var, key_var, ckey_var, auto_var, auto_update_var):
        var.trace_add("write", lambda *a: persist_settings())
    frame.columnconfigure(1, weight=1)

    # ── 日志区（双 tab：启动器/API 日志 + ComfyUI 日志）──
    log_frame = tk.LabelFrame(root, text=" 实时日志 ", padx=4, pady=4, font=("Microsoft YaHei UI", 9))
    log_frame.pack(fill="both", expand=True, padx=10, pady=4)

    log_nb = ttk.Notebook(log_frame)
    log_nb.pack(fill="both", expand=True)

    def _make_log_page():
        page = tk.Frame(log_nb, bg="#1e1e1e")
        text = scrolledtext.ScrolledText(
            page, height=16, font=("Consolas", 9), state="disabled",
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
        )
        text.pack(fill="both", expand=True)
        return page, text

    launcher_page, launcher_text = _make_log_page()
    comfy_page, comfy_text = _make_log_page()
    log_nb.add(launcher_page, text=" 启动器 / API ")
    log_nb.add(comfy_page, text="  ComfyUI  ")

    def _append_to(text, level, line):
        tag = "INFO" if level in ("INFO", "COMFY") else ("WARN" if level == "WARN" else "ERROR")
        text.configure(state="normal")
        text.insert("end", line + "\n", tag)
        text.tag_config(tag, foreground="#ffffff" if level == "COMFY" else (
            "#ffcc00" if level == "WARN" else "#ff6666"))
        text.see("end")
        text.configure(state="disabled")

    def _append(level, line, source="launcher"):
        _append_to(launcher_text if source == "launcher" else comfy_text, level, line)

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
    btn_frame.pack(fill="x")

    def _run_in_thread(fn):
        threading.Thread(target=fn, daemon=True).start()

    def do_start():
        persist_settings()
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
            root.after(0, lambda: messagebox.showerror("启动失败", r.get("error", "未知错误")))
        else:
            root.after(0, lambda: _append("INFO", f"✓ {r.get('message')}"))

    def do_stop():
        persist_settings()
        if messagebox.askyesno("确认停止", "强制结束 ComfyUI 进程树？"):
            _run_in_thread(lambda: _do_stop())

    def _do_stop():
        r = _manager.stop()
        root.after(0, lambda: _append("INFO", f"✓ {r.get('message')}"))

    def do_restart():
        persist_settings()
        if messagebox.askyesno("确认重启", "将强制停止后重新启动 ComfyUI，确定？"):
            _run_in_thread(lambda: _do_restart())

    def _do_restart():
        r = _manager.restart(load_config())
        if not r.get("ok"):
            root.after(0, lambda: messagebox.showerror("重启失败", r.get("error", "未知错误")))
        else:
            root.after(0, lambda: _append("INFO", f"✓ {r.get('message')}"))

    def do_check_update():
        def _worker():
            try:
                st = check_update(verbose=True)
                if not st.get("ok"):
                    root.after(0, lambda: _append("WARN", f"⚠ 检查更新失败: {st.get('reason', '')}"))
                    return
                if not st.get("has_update"):
                    root.after(0, lambda: _append("INFO", f"✓ {st.get('reason', '')} (v{st.get('current', '')})"))
                    return
                msg = f"发现新版本 v{st.get('latest')}\n当前 v{st.get('current')}"
                cl = st.get("changelog")
                if cl:
                    msg += f"\n\n更新说明:\n{cl}"
                if not messagebox.askyesno("发现新版本", msg + "\n\n是否立即更新？"):
                    return
                r = apply_update()
                if r.get("ok"):
                    root.after(0, lambda: _append("INFO", f"✓ 更新完成，正在重启... v{st.get('latest')}"))
                    root.after(1500, restart_self)
                else:
                    root.after(0, lambda: messagebox.showerror("更新失败", r.get("error", "未知错误")))
            except Exception as e:
                root.after(0, lambda: _append("ERROR", f"✗ 检查更新异常: {e}"))

        _run_in_thread(_worker)

    tk.Button(btn_frame, text="🚀 启动", command=do_start, bg="#0a7d32", fg="white",
              font=("Microsoft YaHei UI", 11, "bold"), padx=18, pady=4).pack(side="left", padx=6)
    tk.Button(btn_frame, text="⏹ 停止", command=do_stop, bg="#c0392b", fg="white",
              font=("Microsoft YaHei UI", 11, "bold"), padx=18, pady=4).pack(side="left", padx=6)
    tk.Button(btn_frame, text="🔄 重启", command=do_restart, bg="#e67e22", fg="white",
              font=("Microsoft YaHei UI", 11, "bold"), padx=18, pady=4).pack(side="left", padx=6)
    tk.Button(btn_frame, text="📥 检查更新", command=do_check_update, bg="#6c5ce7", fg="white",
              font=("Microsoft YaHei UI", 11, "bold"), padx=18, pady=4).pack(side="left", padx=6)

    hint = tk.Label(root, text=f"管理地址: http://{cfg['listen_host']}:{cfg['listen_port']}   "
                               "(仅本机，密钥见上方设置)",
                    fg="#888", font=("Microsoft YaHei UI", 9))
    hint.pack(fill="x", padx=10, pady=(0, 6))

    root.after(200, pump_logs)
    root.after(500, refresh_status)
    _log("INFO", f"GUI 就绪，管理端口 {cfg['listen_port']}")
    root.mainloop()


# ══════════════════════════════════════════════════════════════
# 更新机制（由壳 exe 调用；core.py 本身可被热替换）
# ══════════════════════════════════════════════════════════════

CORE_VERSION = "1.0.1"
# 更新源：GitHub 加速镜像列表 + 仓库地址。version.json 与 core.py 放同目录。
# 支持 GitHub 文件加速（gh-proxy 等）：prefix + /https://raw.githubusercontent.com/...
UPDATE_CONFIG = {
    "mirrors": [
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


def _load_update_config(cfg: dict) -> dict:
    """合并用户配置覆盖：config.json 里可写 update.mirrors/repo/branch 覆盖默认。"""
    u = cfg.get("update") or {}
    merged = json.loads(json.dumps(UPDATE_CONFIG))
    if u.get("mirrors"):
        merged["mirrors"] = u["mirrors"]
    for k in ("repo", "branch", "version_file", "core_file", "timeout"):
        if u.get(k) is not None:
            merged[k] = u[k]
    return merged


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _http_get_bytes(url: str, timeout: int) -> Optional[bytes]:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "comfyui-launcher/updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_version_info(ucfg: dict) -> Optional[dict]:
    for mirror in ucfg["mirrors"]:
        url = f"{mirror}/{ucfg['repo']}/{ucfg['branch']}/{ucfg['version_file']}"
        try:
            data = _http_get_bytes(url, int(ucfg.get("timeout", 15)))
            return json.loads(data.decode("utf-8"))
        except Exception:
            continue
    return None


def _download_core(ucfg: dict) -> Optional[bytes]:
    for mirror in ucfg["mirrors"]:
        url = f"{mirror}/{ucfg['repo']}/{ucfg['branch']}/{ucfg['core_file']}"
        try:
            return _http_get_bytes(url, int(ucfg.get("timeout", 15)))
        except Exception:
            continue
    return None


def check_update(verbose: bool = False) -> dict:
    """检查更新。返回 {ok, has_update, current, latest, changelog, reason}。

    逻辑：本地 core.py sha256 对比远端 version.json 的 core_sha256，不一致即有更新。
    """
    ucfg = _load_update_config(load_config())
    info = _fetch_version_info(ucfg)
    if not info:
        return {"ok": False, "has_update": False, "reason": "无法获取版本信息（网络或镜像失效）"}
    local_hash = _sha256_file(CORE_PATH)
    remote_hash = str(info.get("core_sha256", "")).strip().lower()
    has = bool(remote_hash) and local_hash.lower() != remote_hash
    return {
        "ok": True,
        "has_update": has,
        "current": CORE_VERSION,
        "latest": str(info.get("version", "")),
        "changelog": str(info.get("changelog", "")),
        "reason": "" if has else "当前已是最新版本",
    }


def apply_update() -> dict:
    """下载新 core.py → sha256 校验 → .tmp 写入 → os.replace 原子替换 → 备份回滚。

    成功返回 {"ok": True, "file": ...}；失败返回 {"ok": False, "error": ...}。
    """
    ucfg = _load_update_config(load_config())
    info = _fetch_version_info(ucfg)
    if not info:
        return {"ok": False, "error": "无法获取版本信息"}
    expected = str(info.get("core_sha256", "")).strip().lower()
    data = _download_core(ucfg)
    if data is None:
        return {"ok": False, "error": "下载 core.py 失败"}
    if expected and hashlib.sha256(data).hexdigest().lower() != expected:
        return {"ok": False, "error": "sha256 校验失败，已拒绝写入（防损坏/篡改）"}
    try:
        bak = CORE_PATH.with_suffix(".py.bak")
        if CORE_PATH.is_file() and not bak.exists():
            os.replace(CORE_PATH, bak)          # 备份当前版本
        tmp = CORE_PATH.with_suffix(".py.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, CORE_PATH)              # 原子替换
        return {"ok": True, "file": str(CORE_PATH), "backup": str(bak)}
    except Exception as e:
        # 回滚：替换失败则恢复备份
        if CORE_PATH.is_file() and bak.is_file():
            try:
                os.replace(bak, CORE_PATH)
            except Exception:
                pass
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


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
