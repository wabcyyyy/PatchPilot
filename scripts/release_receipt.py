"""G16 发布收据生成器:release-receipt.json + 逐场景结果 + 证据索引输入。

SPEC G16:"产出 release-receipt.json、逐场景结果、测试日志、演示文字步骤、证据
索引与已知限制;缺 Docker 集成证据则标 release_incomplete_for_container_execution。"

本脚本在**发布节点**(批次退出)运行一次,产物落 docs/evidence/agent-upgrade/:
- 逐场景结果:六个离线演示 subprocess 真跑(可用 --skip-demos 跳过,如实标注);
- 环境/身份:解释器与工具版本、git commit 与脏文件清单(脏 = 声明环境不纯,如实记);
- 全量测试收据:调用方用 --pytest-receipt 传入(JSON:命令/退出码/通过数/耗时/日志),
  脚本不代跑全量——那是批次退出的既有节奏,缺收据时 receipt 如实标 missing;
- 已知限制:静态清单与登记的差距对齐;容器执行证据缺失显式打标。

收据只登记事实:部分通过不得写成全量通过,缺什么标什么。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence" / "agent-upgrade"

RELEASE_RECEIPT_SCHEMA_VERSION = 1

# 已知限制:与 SPEC/进度文件登记的差距对齐;每条都要能回答"这是什么没证明"
KNOWN_LIMITATIONS = [
    "离线收据只证协议与边界:修复率、轮次/成本收益、窗口配比最优性属于 G17 真实端点批,"
    "FakeLLM 的 resolved 数不得宣读为真实修复率",
    "repair_v2 的 64k 目标窗口/keep=12、max_identical_failures=3、委派并发=2/每任务 3 次"
    "是工程起点,无真实负载读数",
    "release_incomplete_for_container_execution:本机无 Docker 守护进程,G09 的容器执行"
    "隔离证据缺失(docker 用例 skip),容器内执行路径未在声明环境内验证",
    "在线 cost/hybrid 预算闸要求价目表与金额配置,离线验收不覆盖真实计费链路",
    "MVP 的上下文容量检查只在显式声明容量时生效(legacy_v1 刻意豁免),未声明容量不承诺",
]

DEMOS = [
    ("dirty-ticket", "demo.run_dirty_ticket"),
    ("api-golden-path", "demo.run_api_ticket"),
    ("issue-only-full-flow", "demo.run_issue_only_ticket"),
    ("steer-and-answer", "demo.run_steer_ticket"),
    ("delegate-subtask", "demo.run_delegate_ticket"),
    ("crash-resume", "demo.run_restart_resume_ticket"),
]


def _git(args: list[str]) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True, check=False
    ).stdout.strip()


def collect_run_identity() -> dict[str, object]:
    dirty = [line for line in _git(["status", "--porcelain"]).splitlines() if line.strip()]
    return {
        "platform_commit": _git(["rev-parse", "HEAD"]),
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "dirty_files": dirty,
        "generated_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
    }


def collect_environment() -> dict[str, object]:
    def _version(module: str) -> str:
        proc = subprocess.run(
            [sys.executable, "-m", module, "--version"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout.strip() or f"error: {proc.stderr.strip()[:80]}"

    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "pytest": _version("pytest"),
        "ruff": _version("ruff"),
    }


def run_demos(out_root: Path) -> list[dict[str, object]]:
    """逐场景 subprocess 真跑;每条记录退出码与耗时,不吞失败。"""
    results: list[dict[str, object]] = []
    for name, module in DEMOS:
        out = out_root / f"demo-{name}"
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", module, "--out", str(out)],
            cwd=str(REPO_ROOT),
            env={
                **__import__("os").environ,
                "PATCHPILOT_LLM_ENABLED": "false",
                "PYTHONIOENCODING": "utf-8",
            },
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        results.append(
            {
                "scenario": name,
                "module": module,
                "exit_code": proc.returncode,
                "ok": proc.returncode == 0,
                "seconds": round(time.monotonic() - started, 1),
            }
        )
    return results


def load_pytest_receipt(path: Path | None) -> dict[str, object]:
    if path is None:
        return {"status": "missing", "notes": "未提供全量测试收据;部分通过不得称为全量通过"}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"status": "recorded", **data}


def collect_evidence_index() -> list[str]:
    if not EVIDENCE_DIR.is_dir():
        return []
    return sorted(
        str(path.relative_to(REPO_ROOT).as_posix())
        for path in EVIDENCE_DIR.iterdir()
        if path.is_file()
    )


def build_receipt(
    *,
    demos: list[dict[str, object]] | None,
    pytest_receipt: dict[str, object],
    identity: dict[str, object],
    environment: dict[str, object],
) -> dict[str, object]:
    demo_ok = bool(demos) and all(item["ok"] for item in demos)
    docker_evidence = any("docker" in name for name in collect_evidence_index())
    return {
        "schema_version": RELEASE_RECEIPT_SCHEMA_VERSION,
        "release": "agent-upgrade-b6-g16",
        "run_identity": identity,
        "environment": environment,
        "scenarios": {
            "demos": demos or [],
            "demos_all_ok": demo_ok,
            "skipped": demos is None,
        },
        "pytest_full_suite": pytest_receipt,
        "container_execution_evidence": {
            "present": docker_evidence,
            "release_incomplete_for_container_execution": not docker_evidence,
        },
        "evidence_index": collect_evidence_index(),
        "known_limitations": list(KNOWN_LIMITATIONS),
        "text_demo_steps": "demo/README.md",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G16 发布收据生成(发布节点运行一次)")
    parser.add_argument(
        "--out", default=str(EVIDENCE_DIR / "release-receipt.json"), help="收据输出路径"
    )
    parser.add_argument(
        "--demo-out",
        default="",
        help="演示产物根目录(默认 runs/release-demo-<时间戳>)",
    )
    parser.add_argument("--skip-demos", action="store_true", help="跳过演示真跑(收据如实标注)")
    parser.add_argument(
        "--pytest-receipt", default="", help="全量测试收据 JSON 路径(命令/退出码/读数)"
    )
    args = parser.parse_args(argv)

    demos: list[dict[str, object]] | None = None
    if not args.skip_demos:
        demo_out = (
            Path(args.demo_out)
            if args.demo_out
            else (REPO_ROOT / "runs" / f"release-demo-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        )
        demo_out.mkdir(parents=True, exist_ok=True)
        demos = run_demos(demo_out)

    receipt = build_receipt(
        demos=demos,
        pytest_receipt=load_pytest_receipt(
            Path(args.pytest_receipt) if args.pytest_receipt else None
        ),
        identity=collect_run_identity(),
        environment=collect_environment(),
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"release receipt -> {out_path}")
    if demos is not None:
        for item in demos:
            mark = "OK  " if item["ok"] else "FAIL"
            print(f"[{mark}] {item['scenario']} ({item['seconds']}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
