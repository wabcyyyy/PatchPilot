"""G05 行为收据:同一份真实工具回执,旧路径(序列化后按字符切)与新路径(结构上裁 + 原文落盘)。

零模型、零网络。素材是本地缓存的**真实上游仓库**文件(`.pytest-tmp*/base-SWE-*/repo`),
不是合成字符串:read_file 的 400 行窗口在真实源码上才会撑破可见上限。

用法:
    .venv/Scripts/python.exe scripts/probe_g05_bounded_view.py [--root=<扫描根>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.context.evidence import EvidenceStore
from app.tools.base import ToolContext
from app.tools.output_filter import fold_output
from app.tools.registry import execute
from app.tools.tracker import Tracker

#: 每个仓库挑一个"必然撑破可见上限"的文件:大源码文件是真实题面里的常客
TARGETS = [
    ("base-SWE-sphinx-doc__sphinx-75900", "sphinx/util/cfamily.py"),
    ("base-SWE-astropy__astropy-87070", "astropy/io/fits/card.py"),
    ("base-SWE-pylint-dev__pylint-69030", "pylint/lint/run.py"),
    ("base-SWE-psf__requests-11420", "requests/adapters.py"),
]


def _find_roots(scan_root: Path) -> list[tuple[str, Path, str]]:
    """每个 (题, 文件) 只取第一个命中的缓存副本:同名仓库在多份临时根里重复存在。"""
    found: list[tuple[str, Path, str]] = []
    seen: set[tuple[str, str]] = set()
    for base in sorted(scan_root.glob(".pytest-tmp*")):
        for wanted, rel in TARGETS:
            repo = base / wanted / "repo"
            if (repo / rel).is_file() and (wanted, rel) not in seen:
                seen.add((wanted, rel))
                found.append((wanted, repo, rel))
    return found


def _ctx(workspace: Path, evidence_root: Path) -> ToolContext:
    store = EvidenceStore(evidence_root / "evidence", "PROBE-G05")
    return ToolContext(
        task_id="PROBE-G05",
        workspace=workspace,
        baseline_commit="",
        tracker=Tracker(None, task_id="PROBE-G05"),
        report_dir=evidence_root,
        evidence=store,
        test_sets={},
        allowed_paths=None,
    )


def measure(workspace: Path, rel: str, tmp: Path, label: str) -> dict[str, Any]:
    """同一份**工具真实回执**分别过旧路径与新路径,把两边读数并排出来。

    回执取自 `execute(read_file)` 本身,不是脚本自造的字典 —— 否则两侧比的可能是两份东西。
    """
    settings = get_settings()
    cap = settings.tool_visible_max_chars
    ctx = _ctx(workspace, tmp)
    result = execute(ctx, "read_file", {"path": rel, "limit": 400})
    if not result.ok:
        raise SystemExit(f"read_file 失败 {workspace}/{rel}: {result.error}")
    raw_json = json.dumps(result.output, ensure_ascii=False, default=str)

    # 旧路径:先序列化,再按行折叠 —— 单行 JSON 让折叠退化成硬切
    legacy = fold_output(raw_json, head=settings.refine_head_lines, tail=settings.refine_tail_lines)
    try:
        json.loads(legacy)
        legacy_valid = True
    except json.JSONDecodeError:
        legacy_valid = False

    bounded_applied = result.visible is not None
    visible_json = json.dumps(
        result.visible if bounded_applied else result.output, ensure_ascii=False, default=str
    )
    try:
        parsed = json.loads(visible_json)
        new_valid = True
    except json.JSONDecodeError:
        parsed = {}
        new_valid = False
    meta = (parsed or {}).get("_bounded") or {}
    ref = result.evidence
    reassembled: bool | None = None
    if ref is not None and ref.retrievable and ctx.evidence is not None:
        collected = ""
        cursor = ""
        for _ in range(500):
            page = ctx.evidence.read(ref.evidence_id, cursor=cursor)
            collected += page.text
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        reassembled = collected == raw_json
    return {
        "file": f"{label}/{rel}",
        "raw_chars": len(raw_json),
        "raw_sha256": hashlib.sha256(raw_json.encode("utf-8")).hexdigest()[:16],
        "visible_max_chars": cap,
        "over_cap": bounded_applied,
        "legacy": {
            "chars": len(legacy),
            "json_valid": legacy_valid,
            "chars_dropped_without_trace": max(0, len(raw_json) - len(legacy)),
            "raw_kept_on_disk": False,
        },
        "bounded": {
            "chars": len(visible_json),
            "json_valid": new_valid,
            "omitted_lines": meta.get("omitted_lines", 0),
            "dropped_items": meta.get("dropped_items", 0),
            "evidence_id": (ref.evidence_id if ref else ""),
            "retrievable": bool(ref.retrievable) if ref else False,
            "raw_reassembles_byte_for_byte": reassembled,
        },
    }


def main() -> int:
    # 本机控制台码页是 GBK,而读数里有中文:显式按 UTF-8 输出,证据文件才能直接复核
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--work", default="", help="证据落盘目录(默认系统临时目录,仓库外)")
    args = parser.parse_args()
    scan_root = Path(args.root).resolve()
    roots = _find_roots(scan_root)
    if not roots:
        print(f"没有找到任何缓存仓库文件,扫过:{scan_root}", file=sys.stderr)
        return 1
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="g05-probe-"))
    rows = []
    for name, repo, rel in roots:
        rows.append(measure(repo, rel, work / name, name))
    print(
        json.dumps(
            {
                "probe": "g05-bounded-view",
                "scanned_root": str(scan_root),
                "evidence_work_dir": str(work),
                "sources": len(rows),
                "over_visible_cap": sum(1 for row in rows if row["over_cap"]),
                "legacy_json_broken": sum(1 for row in rows if not row["legacy"]["json_valid"]),
                "bounded_json_valid_all": all(row["bounded"]["json_valid"] for row in rows),
                "raw_reassembled_all": all(
                    row["bounded"]["raw_reassembles_byte_for_byte"] is True
                    for row in rows
                    if row["over_cap"]
                ),
                "note": "零模型零网络;素材为本地缓存的真实上游仓库源码",
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
