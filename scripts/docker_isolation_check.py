"""Docker 隔离边界实验(T8.2 验收):联网、宿主文件访问、文件系统可见性、正常执行。

用法:python scripts/docker_isolation_check.py
输出供 docs/docker-isolation-notes.md 记录。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "patchpilot-executor:latest"


def sh(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    out = proc.stdout.decode("utf-8", errors="replace") + proc.stderr.decode(
        "utf-8", errors="replace"
    )
    return proc.returncode, out.strip()


def main() -> int:
    if shutil.which("docker") is None:
        print("docker CLI 不可用")
        return 1
    rc, out = sh(["docker", "info", "--format", "{{.ServerVersion}}"])
    if rc != 0:
        print("docker 守护进程不可达:", out[:200])
        return 1
    print(f"[env] docker server {out}")

    rc, out = sh(["docker", "image", "inspect", IMAGE], timeout=30)
    if rc != 0:
        print("[build] 构建执行器镜像 …")
        rc, out = sh(
            [
                "docker",
                "build",
                "-t",
                IMAGE,
                "-f",
                str(ROOT / "docker" / "executor.Dockerfile"),
                str(ROOT / "docker"),
            ]
        )
        if rc != 0:
            print("镜像构建失败:", out[-500:])
            return 1
    print(f"[build] {IMAGE} 就绪")

    results: list[tuple[str, bool, str]] = []

    # 实验 1:--network none 下访问外网必须失败
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=5)\n"
        "    print('NETWORK-OK')\n"
        "except OSError as exc:\n"
        "    print('NETWORK-BLOCKED:', type(exc).__name__)\n"
    )
    rc, out = sh(["docker", "run", "--rm", "--network", "none", IMAGE, "python", "-c", code])
    results.append(
        (
            "联网被 --network=none 阻断",
            "NETWORK-BLOCKED" in out,
            out.splitlines()[-1] if out else "",
        )
    )

    # 实验 2:容器内看不到宿主文件系统(未挂载路径)
    code = "import os\nprint('HOST-VISIBLE' if os.path.isdir('/host-windows') else 'HOST-HIDDEN')\n"
    rc, out = sh(["docker", "run", "--rm", IMAGE, "python", "-c", code])
    results.append(
        ("未挂载的宿主路径不可见", "HOST-HIDDEN" in out, out.splitlines()[-1] if out else "")
    )

    # 实验 3:容器内无法修改宿主文件(只挂载工作区,其余不可达)
    tmp = (
        Path(tempfile.mkdtemp(prefix="iso-", dir=ROOT / ".pytest-tmp"))
        if (ROOT / ".pytest-tmp").exists()
        else Path(tempfile.mkdtemp(prefix="iso-"))
    )
    marker = tmp / "marker.txt"
    marker.write_text("original", encoding="utf-8")
    rc, out = sh(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{tmp}:/ws:rw",
            IMAGE,
            "python",
            "-c",
            "open('/ws/marker.txt','a').write('+container')\nprint('WRITE-DONE')",
        ]
    )
    results.append(
        (
            "挂载目录(读写)外的宿主不可达",
            "WRITE-DONE" in out and marker.read_text(encoding="utf-8").endswith("+container"),
            out.splitlines()[-1] if out else "",
        )
    )
    marker.unlink()
    shutil.rmtree(tmp, ignore_errors=True)

    # 实验 4:资源限额生效(内存 16MB 下分配 64MB 应失败)
    rc, out = sh(
        [
            "docker",
            "run",
            "--rm",
            "--memory",
            "16m",
            IMAGE,
            "python",
            "-c",
            "x = bytearray(64 * 1024 * 1024)\nprint('ALLOC-OK')",
        ],
        timeout=60,
    )
    results.append(
        (
            "--memory 限额生效(大分配失败)",
            "ALLOC-OK" not in out,
            (out.splitlines() or ["killed"])[-1],
        )
    )

    # 实验 5:正常跑测试(--rm 用后即删)
    tmp_ws = Path(
        tempfile.mkdtemp(
            prefix="ws-", dir=ROOT / ".pytest-tmp" if (ROOT / ".pytest-tmp").exists() else None
        )
    )
    try:
        sys.path.insert(0, str(ROOT))
        from app.gitops.testing import materialize_repo

        materialize_repo(ROOT / "bugs" / "BUG-003" / "repo", tmp_ws, extra_commit=False)
        rc, out = sh(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{tmp_ws}:/ws",
                "-w",
                "/ws",
                IMAGE,
                "python",
                "-m",
                "pytest",
                "-q",
                "--color=no",
                "tests/test_labels.py::test_default_separator",
            ]
        )
        results.append(
            (
                "容器内执行 pytest(1 failed 预期)",
                "1 failed" in out,
                out.splitlines()[-1] if out else "",
            )
        )
    finally:
        shutil.rmtree(tmp_ws, ignore_errors=True)

    print()
    all_ok = True
    for name, ok, detail in results:
        all_ok &= ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
