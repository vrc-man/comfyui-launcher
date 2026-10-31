# -*- coding: utf-8 -*-
"""
发布工具：生成 version.json（版本 + core.py sha256 + exe 信息 + 更新说明）。

用法：
  python make_version.py                       # 读 core.py 的 CORE_VERSION，自动算哈希
  python make_version.py 1.2.0                 # 指定版本号
  python make_version.py 1.2.0 "更新说明"
  python make_version.py 1.2.0 "更新说明" raw   # exe 放仓库里（覆盖 comfy_launcher.exe 后提交）
  python make_version.py 1.2.0 "更新说明" "https://任意直链/comfy_launcher.exe"

产物：
  version.json — 提交到 GitHub 仓库根目录，启动器据此对比哈希判断 core.py / exe 是否有更新

exe 的两种托管方式：
  raw      —— 把 dist\\comfy_launcher.exe 复制到仓库根目录 comfy_launcher.exe 并随代码提交，
              下载地址为 https://raw.githubusercontent.com/<repo>/<branch>/comfy_launcher.exe
              （和以前更新 core.py 一样的方式；注意每次发版会让仓库历史 +18MB）
  Release  —— exe 作为 Release 附件（默认），仓库里只有文本文件

字段说明：
  core_sha256 / core_size   热更新的核心逻辑哈希（对比本地 core.py）
  exe.sha256 / exe.size     启动器本体的哈希（对比本地正在运行的 exe）
  exe.url                   本体下载地址；未指定时按仓库+版本号自动拼 Release 地址
"""

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CORE_PATH = BASE_DIR / "core.py"
EXE_PATH = BASE_DIR / "dist" / "comfy_launcher.exe"
EXE_REPO_PATH = BASE_DIR / "comfy_launcher.exe"      # raw 模式下仓库里的本体位置
VERSION_JSON = BASE_DIR / "version.json"
DEFAULT_REPO = "vrc-man/comfyui-launcher"
DEFAULT_BRANCH = "main"
EXE_ASSET = "comfy_launcher.exe"


def read_core_version() -> str:
    text = CORE_PATH.read_text(encoding="utf-8")
    m = re.search(r'^CORE_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
    return m.group(1) if m else "0.0.0"


def _read_update_field(name: str, default: str) -> str:
    """从 core.py 的 UPDATE_CONFIG 里读仓库/分支，保证三处一致。"""
    try:
        text = CORE_PATH.read_text(encoding="utf-8")
        m = re.search(r'"%s"\s*:\s*"([^"]+)"' % re.escape(name), text)
        if m:
            return m.group(1)
    except OSError:
        pass
    return default


def read_repo() -> str:
    return _read_update_field("repo", DEFAULT_REPO)


def read_branch() -> str:
    return _read_update_field("branch", DEFAULT_BRANCH)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if not CORE_PATH.is_file():
        print(f"[ERROR] 找不到 {CORE_PATH}")
        return 1

    version = read_core_version()
    changelog = ""
    exe_url = ""
    if len(sys.argv) >= 2:
        version = sys.argv[1]
    if len(sys.argv) >= 3:
        changelog = sys.argv[2]
    if len(sys.argv) >= 4:
        exe_url = sys.argv[3]

    repo, branch = read_repo(), read_branch()
    data = {
        "version": version,
        "core_sha256": sha256_of(CORE_PATH),
        "core_size": CORE_PATH.stat().st_size,
        "changelog": changelog,
    }

    if EXE_PATH.is_file():
        mode = "release"
        if exe_url.lower() == "raw":
            mode = "raw"
            # 关键：先把这个 exe 复制成仓库里那份，再算哈希 ——
            # 「被提交的文件」和「被哈希的文件」必须是同一个字节，否则用户端校验必失败
            shutil.copy2(EXE_PATH, EXE_REPO_PATH)
            exe_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{EXE_ASSET}"
        elif not exe_url:
            exe_url = (f"https://github.com/{repo}/releases/download/"
                       f"v{version}/{EXE_ASSET}")
        exe_file = EXE_REPO_PATH if mode == "raw" else EXE_PATH
        data["exe"] = {
            "sha256": sha256_of(exe_file),
            "size": exe_file.stat().st_size,
            "url": exe_url,
        }
        if mode == "raw":
            print(f"[INFO] 已把本体复制到仓库: {EXE_REPO_PATH}（记得 git add 提交）")
    else:
        print(f"[WARN] 未找到 {EXE_PATH}，version.json 只包含 core.py 信息"
              f"（老启动器无法检测本体更新）")

    VERSION_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] version.json 已生成: {VERSION_JSON}")
    print(f"     version      = {version}")
    print(f"     core_sha256  = {data['core_sha256']}")
    print(f"     core_size    = {data['core_size']} bytes")
    if data.get("exe"):
        print(f"     exe.sha256   = {data['exe']['sha256']}")
        print(f"     exe.size     = {data['exe']['size']} bytes")
        print(f"     exe.url      = {data['exe']['url']}")
    if changelog:
        print(f"     changelog    = {changelog}")
    print("\n提交推送后，启动器即可检测到更新。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

