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
import re                # noqa: F401
import urllib.request    # noqa: F401
from typing import Optional   # noqa: F401
from collections import deque  # noqa: F401
from datetime import datetime  # noqa: F401
import tkinter as tk            # noqa: F401
from tkinter import ttk, scrolledtext, filedialog, messagebox  # noqa: F401
try:                              # 依赖缺失时交给 core.py 给出中文提示，壳不先崩
    import fastapi                  # noqa: F401
    from fastapi import Header, HTTPException, Query  # noqa: F401
    from fastapi.responses import JSONResponse  # noqa: F401
    import uvicorn                   # noqa: F401
except ImportError:
    pass

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

CORE_PATH = BASE_DIR / "core.py"
LOCK_PATH = BASE_DIR / "comfy_launcher.lock"
_LOCK_FH = None          # 单实例锁句柄（必须常驻，否则锁随 GC 释放）

# 自动更新：启动时检查一次（可在 comfy_launcher_config.json 配置 update.auto 开启）
# 与 README/GUI 一致：默认关闭，只能用「检查更新」按钮手动更新。
AUTO_UPDATE_DEFAULT = False


def _load_core():
    """从 exe 同目录动态加载 core.py。失败抛出明确错误。"""
    if not CORE_PATH.is_file():
        raise SystemExit(f"缺少核心文件: {CORE_PATH}\n请把 core.py 与启动器放在同一目录。")
    spec = importlib.util.spec_from_file_location("core_module", CORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _acquire_single_instance() -> None:
    """单实例锁：用 msvcrt 锁定锁文件，抢不到则提示并退出。

    注意：文件句柄必须挂在模块级全局变量上。旧实现把 fh 留在局部变量里，
    函数返回后 CPython 立刻回收 -> 文件关闭 -> 锁释放，多开完全不受限。
    """
    global _LOCK_FH
    try:
        fh = open(LOCK_PATH, "a+")
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            _LOCK_FH = fh          # 进程存活期间持有，退出时由 OS 释放
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
    """启动时自动检测更新。返回 True 表示已触发更新（进程将重启/退出）。

    更新分两条通道，全部交给 core.run_startup_update() 处理：
      - core.py 热更新（替换文件后重启）
      - 启动器本体 exe 更新（后台脚本等本进程退出后换文件再拉起）
    """
    try:
        # utf-8-sig：配置被记事本保存成带 BOM 的 UTF-8 时不至于解析失败
        cfg = json.loads(CORE_PATH.with_name("comfy_launcher_config.json")
                         .read_text(encoding="utf-8-sig"))
        auto = bool(cfg.get("update", {}).get("auto", AUTO_UPDATE_DEFAULT))
    except Exception:
        auto = AUTO_UPDATE_DEFAULT
    if not auto:
        return False
    try:
        return bool(core.run_startup_update())
    except SystemExit:
        return True          # 更新流程里可能直接退出进程
    except Exception:
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
