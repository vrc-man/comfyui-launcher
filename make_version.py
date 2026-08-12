# -*- coding: utf-8 -*-
"""
发布工具：生成 version.json（版本 + core.py sha256 哈希 + 更新说明）。

用法：
  python make_version.py                 # 读取 core.py 的 CORE_VERSION，自动算哈希
  python make_version.py 1.2.0           # 指定新版本号（可选，不指定则用 core.py 里的 CORE_VERSION）
  python make_version.py 1.2.0 "更新说明文字"

产物：
  version.json  —— 提交到 GitHub 仓库，启动器据此对比哈希判断是否更新
"""

import hashlib
import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CORE_PATH = BASE_DIR / "core.py"
VERSION_JSON = BASE_DIR / "version.json"


def read_core_version() -> str:
    text = CORE_PATH.read_text(encoding="utf-8")
    m = re.search(r'^CORE_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
    return m.group(1) if m else "0.0.0"


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
    if len(sys.argv) >= 2:
        version = sys.argv[1]
    if len(sys.argv) >= 3:
        changelog = sys.argv[2]

    core_sha = sha256_of(CORE_PATH)
    data = {
        "version": version,
        "core_sha256": core_sha,
        "core_size": CORE_PATH.stat().st_size,
        "changelog": changelog,
    }
    VERSION_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] version.json 已生成: {VERSION_JSON}")
    print(f"     version      = {version}")
    print(f"     core_sha256  = {core_sha}")
    print(f"     core_size    = {data['core_size']} bytes")
    if changelog:
        print(f"     changelog    = {changelog}")
    print("\n提交推送后，启动器将自动检测到更新。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
