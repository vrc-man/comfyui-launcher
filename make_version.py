# -*- coding: utf-8 -*-
"""
发布工具：生成 version.json（版本 + core.py sha256 + exe 信息 + 更新说明）。

用法：
  python make_version.py                       # 读 core.py 的 CORE_VERSION，自动算哈希
  python make_version.py bump                  # 先把 core.py 的 CORE_VERSION 补丁号 +1，再生成
  python make_version.py bump "更新说明" raw    # 递增版本 + 写说明 + 本体随仓库提交

  python make_version.py 1.2.0 "更新说明"       # 指定版本号
  python make_version.py 1.2.0 "更新说明" raw   # exe 放仓库里（覆盖 comfy_launcher.exe 后提交）
  python make_version.py 1.2.0 "更新说明" "https://任意直链/comfy_launcher.exe"

产物：
  version.json — 提交到 GitHub 仓库根目录，启动器据此对比哈希判断 core.py / exe 是否有更新
  同时会把 core.py / version.json 同步到 dist\\，保证打包目录永远和仓库一致

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
DIST_DIR = BASE_DIR / "dist"
DEFAULT_REPO = "vrc-man/comfyui-launcher"
DEFAULT_BRANCH = "main"
EXE_ASSET = "comfy_launcher.exe"
BUMP_KEYWORDS = ("bump", "--bump", "-b")


def read_core_version() -> str:
    text = CORE_PATH.read_text(encoding="utf-8")
    m = re.search(r'^CORE_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
    return m.group(1) if m else "0.0.0"


def bump_core_version(part: str = "patch") -> str:
    """把 core.py 里的 CORE_VERSION 递增（默认补丁号），返回新版本号。

    只替换那一行，字节级别改动最小；行尾格式保持不变。
    """
    text = CORE_PATH.read_text(encoding="utf-8")
    m = re.search(r'^CORE_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        raise SystemExit("[ERROR] core.py 里找不到 CORE_VERSION")
    nums = [int(x) if x.isdigit() else 0 for x in m.group(1).split(".")]
    while len(nums) < 3:
        nums.append(0)
    if part == "major":
        nums = [nums[0] + 1, 0, 0]
    elif part == "minor":
        nums = [nums[0], nums[1] + 1, 0]
    else:
        nums = [nums[0], nums[1], nums[2] + 1]
    new_ver = ".".join(str(n) for n in nums)
    new_text = text[:m.start(1)] + new_ver + text[m.end(1):]
    CORE_PATH.write_text(new_text, encoding="utf-8", newline="")
    print(f"[INFO] CORE_VERSION: {m.group(1)} -> {new_ver}")
    return new_ver


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

    # 参数解析：既支持位置参数（老用法），也支持显式 flag（build.bat 用，避免空参数错位）
    #   --bump[=patch|minor|major]  先把 core.py 的 CORE_VERSION 递增
    #   --raw                       exe 随仓库提交（复制到仓库根 + raw 下载地址）
    #   --changelog=文本 / --note=文本
    args = list(sys.argv[1:])
    bump_part = ""
    force_raw = False
    changelog = ""
    exe_url = ""
    positional = []
    for a in args:
        low = a.lower()
        if low in BUMP_KEYWORDS:
            bump_part = "patch"
        elif low.startswith(("--bump=", "bump:")):
            bump_part = a.split("=", 1)[1] if "=" in a else a.split(":", 1)[1]
            bump_part = (bump_part or "patch").strip()
        elif low in ("raw", "--raw"):
            force_raw = True
        elif low.startswith(("--changelog=", "--note=")):
            changelog = a.split("=", 1)[1]
        elif low.startswith(("http://", "https://")):
            exe_url = a
        else:
            positional.append(a)

    version = bump_core_version(bump_part) if bump_part else read_core_version()
    if positional:
        version = positional[0].strip() or version
    if len(positional) >= 2 and not changelog:
        changelog = positional[1]
    if len(positional) >= 3 and not exe_url:
        exe_url = positional[2]

    repo, branch = read_repo(), read_branch()
    data = {
        "version": version,
        "core_sha256": sha256_of(CORE_PATH),
        "core_size": CORE_PATH.stat().st_size,
        "changelog": changelog,
    }

    if EXE_PATH.is_file():
        mode = "release"
        if force_raw or exe_url.lower() == "raw":
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

    # 同步到 dist/：打包目录里的 core.py / version.json 永远和仓库一致，避免"发出去的还是旧清单"
    if DIST_DIR.is_dir():
        try:
            shutil.copy2(CORE_PATH, DIST_DIR / "core.py")
            shutil.copy2(VERSION_JSON, DIST_DIR / "version.json")
            print(f"[INFO] 已同步到 dist: core.py + version.json")
        except OSError as e:
            print(f"[WARN] 同步 dist 失败: {e}")
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

