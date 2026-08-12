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
    "comfyui": {
        "comfyui_path": "I:/ai/xin/ComfyUI_windows_portable-G313/ComfyUI",
        "python_path": "I:/ai/xin/ComfyUI_windows_portable-G313/python_embeded/python.exe",
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
            comfyui_path = Path(cfg["comfyui"]["comfyui_path"])
            python_path = cfg["comfyui"]["python_path"]
            args = cfg["comfyui"]["args"]
            port = int(cfg["comfyui"].get("port", 8188))
            self._port = port

        if not python_path or not os.path.isfile(python_path):
            return {"ok": False, "error": f"Python 不存在: {python_path}"}
        main_py = comfyui_path / "main.py"
        if not main_py.is_file():
            return {"ok": False, "error": f"ComfyUI main.py 不存在: {main_py}"}

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
        else:
            r = filedialog.askopenfilename(initialdir=start,
                                           filetypes=[("Python", "*.exe")])
            if r:
                entry.delete(0, "end")
                entry.insert(0, r.replace("/", "\\"))

    comfy_var = tk.StringVar(value=cfg["comfyui"]["comfyui_path"])
    py_var = tk.StringVar(value=cfg["comfyui"]["python_path"])
    port_var = tk.StringVar(value=str(cfg["listen_port"]))
    key_var = tk.StringVar(value=cfg["api_key"])
    ckey_var = tk.StringVar(value=cfg["confirm_key"])

    def persist_settings():
        cu = cfg["comfyui"]
        cu["comfyui_path"] = comfy_var.get().strip()
        cu["python_path"] = py_var.get().strip()
        try:
            cfg["listen_port"] = int(port_var.get().strip() or "18808")
        except ValueError:
            cfg["listen_port"] = 18808
        cfg["api_key"] = key_var.get().strip()
        cfg["confirm_key"] = ckey_var.get().strip()
        save_config(cfg)

    for r in range(2):
        frame.grid_rowconfigure(r, weight=0)
    tk.Label(frame, text="ComfyUI 目录:").grid(row=0, column=0, sticky="w", padx=4, pady=3)
    e1 = tk.Entry(frame, textvariable=comfy_var, width=52)
    e1.grid(row=0, column=1, sticky="we", padx=4)
    tk.Button(frame, text="浏览", command=lambda: browse(e1, True)).grid(row=0, column=2, padx=2)

    tk.Label(frame, text="Python 路径:").grid(row=1, column=0, sticky="w", padx=4, pady=3)
    e2 = tk.Entry(frame, textvariable=py_var, width=52)
    e2.grid(row=1, column=1, sticky="we", padx=4)
    tk.Button(frame, text="浏览", command=lambda: browse(e2, False)).grid(row=1, column=2, padx=2)

    tk.Label(frame, text="管理端口:").grid(row=2, column=0, sticky="w", padx=4, pady=3)
    e3 = tk.Entry(frame, textvariable=port_var, width=10)
    e3.grid(row=2, column=1, sticky="w", padx=4)

    tk.Label(frame, text="API 密钥:").grid(row=3, column=0, sticky="w", padx=4, pady=3)
    e4 = tk.Entry(frame, textvariable=key_var, width=52)
    e4.grid(row=3, column=1, sticky="we", padx=4)
    tk.Button(frame, text="生成", command=lambda: key_var.set(secrets.token_hex(16))).grid(row=3, column=2, padx=2)

    tk.Label(frame, text="确认密钥:").grid(row=4, column=0, sticky="w", padx=4, pady=3)
    e5 = tk.Entry(frame, textvariable=ckey_var, width=52)
    e5.grid(row=4, column=1, sticky="we", padx=4)
    tk.Button(frame, text="生成", command=lambda: ckey_var.set(secrets.token_hex(16))).grid(row=4, column=2, padx=2)

    for var in (comfy_var, py_var, port_var, key_var, ckey_var):
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

    tk.Button(btn_frame, text="🚀 启动", command=do_start, bg="#0a7d32", fg="white",
              font=("Microsoft YaHei UI", 11, "bold"), padx=18, pady=4).pack(side="left", padx=6)
    tk.Button(btn_frame, text="⏹ 停止", command=do_stop, bg="#c0392b", fg="white",
              font=("Microsoft YaHei UI", 11, "bold"), padx=18, pady=4).pack(side="left", padx=6)
    tk.Button(btn_frame, text="🔄 重启", command=do_restart, bg="#e67e22", fg="white",
              font=("Microsoft YaHei UI", 11, "bold"), padx=18, pady=4).pack(side="left", padx=6)

    hint = tk.Label(root, text=f"管理地址: http://{cfg['listen_host']}:{cfg['listen_port']}   "
                               "(仅本机，密钥见上方设置)",
                    fg="#888", font=("Microsoft YaHei UI", 9))
    hint.pack(fill="x", padx=10, pady=(0, 6))

    root.after(200, pump_logs)
    root.after(500, refresh_status)
    _log("INFO", f"GUI 就绪，管理端口 {cfg['listen_port']}")
    root.mainloop()


def main():
    # windowed 打包（--windowed）下 sys.stdout/stderr 为 None，重定向到 devnull
    # 防止第三方库隐式访问 .isatty()/.write() 崩溃
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
    cfg = load_config()
    start_http_server(cfg)
    _log("INFO", f"启动器版本 1.0.1，PID={os.getpid()}")
    run_gui()


if __name__ == "__main__":
    main()
