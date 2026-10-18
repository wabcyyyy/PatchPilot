"""Generate a scripted two-attempt repair with real local pytest execution, at zero model cost.

Run from the repository root: python -m scripts.sample_run
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from app.evals.bugset import load_bug, load_replay_script
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM


def sample_script() -> list[dict[str, Any]]:
    bug = load_bug("BUG-001")
    script = load_replay_script(bug, kind="graph")
    wrong = (
        "*** Begin Patch\n*** Update File: src/dateparse.py\n"
        "     if value is None:\n         return None\n"
        '+    if value == "":\n+        return None\n'
        "*** End Patch\n"
    )
    correction = {
        "tool": "apply_patch",
        "args": {
            "patch_text": '*** Begin Patch\n*** Update File: src/dateparse.py\n-    if value == "":\n+    if not value.strip():\n         return None\n*** End Patch\n'
        },
    }
    return [
        {"tool": "find_symbol", "args": {"name": "parse_date", "include_details": True}},
        *script[:3],
        {"tool": "apply_patch", "args": {"patch_text": wrong}},
        {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {
            "tool": "finish",
            "args": {
                "success": True,
                "summary": "Submit first candidate for independent verification",
            },
        },
        correction,
        *script[4:],
    ]


def generate_sample(out: Path, runs_root: Path) -> Path:
    from app.config import get_settings

    if get_settings().execution_backend != "local":
        raise ValueError("This sample requires PATCHPILOT_EXECUTION_BACKEND=local")
    if (runs_root / "sample-engineering-upgrade").exists():
        raise FileExistsError("Choose a fresh --runs-root; existing run evidence is retained")
    if (out / "trajectory.json").exists():
        raise FileExistsError("Choose a fresh --out; existing sample evidence is retained")
    result = run_task_graph(
        load_bug("BUG-001"),
        FakeLLM(sample_script()),
        runs_root=runs_root,
        max_rounds=3,
        task_id="sample-engineering-upgrade",
    )
    if result.verdict != "resolved":
        raise RuntimeError(f"sample did not resolve: {result.status}: {result.error}")
    run_dir = Path(result.run_dir)
    if result.artifact_errors:
        raise RuntimeError(f"sample artifacts incomplete: {result.artifact_errors}")
    out.mkdir(parents=True, exist_ok=True)
    for name in ("patch.diff", "trajectory.json", "evaluation_report.json", "report.json"):
        shutil.copy2(run_dir / name, out / name)
    for name in ("attempts", "reports", "evidence", "candidates"):
        if (run_dir / name).is_dir():
            shutil.copytree(run_dir / name, out / name, dirs_exist_ok=True)
    (out / "replay_script.json").write_text(
        json.dumps(sample_script(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/reproduced-sample"))
    parser.add_argument("--runs-root", type=Path, default=Path("runs/sample-generation"))
    args = parser.parse_args()
    print(generate_sample(args.out, args.runs_root))


if __name__ == "__main__":
    main()
