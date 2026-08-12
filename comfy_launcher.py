# -*- coding: utf-8 -*-
"""
ComfyUI 启动器 — 壳（可执行入口，打包为 exe）。

职责（尽量保持最小，便于不重打包）：
  1. 定位 exe 所在目录（BASE_DIR）
  2. 单实例锁（防止多开）
  3. 从同目录动态加载 core.py（主体逻辑）
  4. 启动时可选自动检测更新（对比 version.json sha256），有更新则下载替换并自动重启
  5. 委托 core.main() 启动 GUI + HTTP 管理服务

更新模型：
  - core.py 是独立文件，可被热替换（哈希检测 → 镜像下载 → 原子替换 → 自动重启）
  - 更新核心逻辑在 core.py（check_update / apply_update / restart_self）
  - 壳只负责加载与调度，几乎不需要重新打包
"""

import os
import sys
import json
import msvcrt
import importlib.util
from pathlib import Path

# ── PyInstaller 依赖锚点 ──
# core.py 由壳在运行时动态加载（importlib），PyInstaller 静态分析看不到其 import。
# 这里显式引入 core 的全部依赖，确保打包进 exe（仅作收集，无实际调用）。
import time              # noqa: F401
import queue             # noqa: F401
import socket            # noqa: F401
import shutil            # noqa: F401
import secrets           # noqa: F401
import hashlib           # noqa: F401
import threading         # noqa: F401
import subprocess        # noqa: F401
import http.server       # noqa: F401
import urllib.request    # noqa: F401
from typing import Optional   # noqa: F401
from collections import deque  # noqa: F401
from datetime import datetime  # noqa: F401
import tkinter as tk            # noqa: F401
from tkinter import ttk, scrolledtext, filedialog, messagebox  # noqa: F401
import fastapi                  # noqa: F401
from fastapi import Header, HTTPException, Query  # noqa: F401
from fastapi.responses import JSONResponse  # noqa: F401
import uvicorn                   # noqa: F401

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

CORE_PATH = BASE_DIR / "core.py"
LOCK_PATH = BASE_DIR / "comfy_launcher.lock"

# 自动更新：启动时检查一次（可在 comfy_launcher_config.json 配置 update.auto 关闭）
AUTO_UPDATE_DEFAULT = True


def _load_core():
    """从 exe 同目录动态加载 core.py。失败抛出明确错误。"""
    if not CORE_PATH.is_file():
        raise SystemExit(f"缺少核心文件: {CORE_PATH}\n请把 core.py 与启动器放在同一目录。")
    spec = importlib.util.spec_from_file_location("core_module", CORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _acquire_single_instance() -> None:
    """单实例锁：用 msvcrt 锁定锁文件，抢不到则提示并退出。"""
    try:
        fh = open(LOCK_PATH, "a+")
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            try:
                import tkinter as tk
                from tkinter import messagebox
                r = tk.Tk()
                r.withdraw()
                messagebox.showinfo("ComfyUI 启动器", "启动器已在运行。")
                r.destroy()
            except Exception:
                pass
            sys.exit(0)
    except (OSError, ImportError):
        pass


def _auto_update_if_needed(core) -> bool:
    """启动时自动检测更新。返回 True 表示已触发更新（进程将重启，本函数后应退出）。"""
    try:
        cfg = json.loads(CORE_PATH.with_name("comfy_launcher_config.json").read_text(encoding="utf-8"))
        auto = cfg.get("update", {}).get("auto", AUTO_UPDATE_DEFAULT)
    except Exception:
        auto = AUTO_UPDATE_DEFAULT
    if not auto:
        return False
    try:
        st = core.check_update()
        if not st.get("ok") or not st.get("has_update"):
            return False
        # 有更新：下载替换 + 自动重启
        r = core.apply_update()
        if r.get("ok"):
            core.restart_self()   # 会 os._exit(0)
            return True
    except Exception:
        pass
    return False


def main():
    # windowed 模式 stdout/stderr 兜底
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
    _acquire_single_instance()
    core = _load_core()
    if _auto_update_if_needed(core):
        return
    core.main()


if __name__ == "__main__":
    main()
