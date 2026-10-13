"""Keep personal iteration quick; exercise selection, reuse and timeout boundaries."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if ROOT.name == "travel-agent-python":
    ROOT = ROOT.parent
TOOLS = ROOT / "scripts"


class PersonalValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ai-validation-guard-")
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        for name in [
            "ai_validate.py",
            "ai_validate_run.py",
            "ai_validate_graph.py",
            "ai_validate_quick.py",
        ]:
            shutil.copyfile(TOOLS / name, self.root / "scripts" / name)
        live = json.loads((ROOT / "validation-rules.json").read_text(encoding="utf-8"))
        self.assertEqual(live["quick_test_files"], 6)
        self.assertEqual(live["quick_budget_seconds"], 90)
        live.update(
            project="fixture",
            python_cwd=".",
            python_prefix="",
            python_executable=sys.executable,
            python_sources=["app/*.py", "tests/*.py", "scripts/*.py"],
            tests=["tests/test_*.py"],
            frontend_related=False,
            frontend="<none>",
            ruff=False,
            groups=[],
            module_static=[{"cwd": ".", "argv": ["MODULE"]}],
            high_risk=["app/leaf.py"],
            full=[{"cwd": ".", "argv": ["FULL"]}],
        )
        self.write("validation-rules.json", json.dumps(live))
        self.write(".gitignore", ".validation/\n__pycache__/\n")
        self.write("app/leaf.py", "value = 1\n")
        self.write("tests/test_leaf.py", "from app.leaf import value\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "-qm",
                "baseline",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        spec = importlib.util.spec_from_file_location(
            "fixture_ai_validate", self.root / "scripts/ai_validate.py"
        )
        assert spec is not None and spec.loader is not None
        self.module: Any = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(self.root / "scripts"))
        try:
            spec.loader.exec_module(self.module)
        finally:
            sys.path.pop(0)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, name: str, text: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def args(self, level: str = "auto", budget: int = 90) -> argparse.Namespace:
        return argparse.Namespace(
            level=level,
            tests=[],
            output=".validation",
            temp_root=self.temp.name,
            timeout=10,
            budget=budget,
            no_cache=False,
        )

    def execute(self, code: str = "pass", budget: int = 90) -> dict[str, Any]:
        result = {
            "quick_mode": True,
            "level": 1,
            "changed": ["app/leaf.py"],
            "manual_required": [],
            "deferred_tests": [],
            "commands": [{"cwd": ".", "argv": [sys.executable, "-c", code]}],
        }
        with contextlib.redirect_stdout(io.StringIO()):
            self.module.execute(result, self.args(budget=budget), self.module.paths())
        receipts = list((self.root / ".validation").glob("*.json"))
        return json.loads(
            max(receipts, key=lambda p: p.stat().st_mtime_ns).read_text(encoding="utf-8")
        )

    def test_staged_rename_is_accepted_by_default_plan(self) -> None:
        subprocess.run(
            ["git", "mv", "app/leaf.py", "app/renamed.py"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["ai_validate"]), contextlib.redirect_stdout(output):
            self.assertEqual(self.module.main(), 0)
        changed = json.loads(output.getvalue())["changed"]
        self.assertIn("app/leaf.py", changed)
        self.assertIn("app/renamed.py", changed)

    def test_staged_deletion_is_accepted_by_default_plan(self) -> None:
        subprocess.run(
            ["git", "rm", "app/leaf.py"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["ai_validate"]), contextlib.redirect_stdout(output):
            self.assertEqual(self.module.main(), 0)
        self.assertIn("app/leaf.py", json.loads(output.getvalue())["changed"])

    def test_high_risk_auto_never_runs_full(self) -> None:
        plan = self.module.plan(self.args(), self.module.paths(), ["app/leaf.py"])
        self.assertEqual(plan["level"], 2)
        self.assertFalse(any("FULL" in c["argv"] for c in plan["commands"]))
        self.assertFalse(any("MODULE" in c["argv"] for c in plan["commands"]))

    def test_module_checks_require_explicit_level_two(self) -> None:
        plan = self.module.plan(self.args("2"), self.module.paths(), ["app/leaf.py"])
        self.assertTrue(any("MODULE" in c["argv"] for c in plan["commands"]))

    def test_full_is_explicit(self) -> None:
        plan = self.module.plan(self.args("3"), self.module.paths(), ["app/leaf.py"])
        self.assertTrue(any("FULL" in c["argv"] for c in plan["commands"]))
        self.assertFalse(plan["quick_mode"])

    def test_missing_mapping_does_not_expand_to_full_or_claim_pass(self) -> None:
        self.write("app/unknown.py", "other = 2\n")
        plan = self.module.plan(self.args(), self.module.paths(), ["app/unknown.py"])
        self.assertTrue(plan["manual_required"])
        self.assertFalse(any("FULL" in c["argv"] for c in plan["commands"]))

    def test_quick_cap_preserves_changed_and_requested_tests(self) -> None:
        self.assertEqual(self.module.CFG["quick_test_files"], 6)
        for number in range(10):
            self.write(f"tests/test_consumer_{number}.py", "from app.leaf import value\n")
        self.write("tests/test_changed.py", "assert True\n")
        args = self.args()
        args.tests = ["tests/test_consumer_9.py::test_node"]
        plan = self.module.plan(args, self.module.paths(), ["app/leaf.py", "tests/test_changed.py"])
        self.assertLessEqual(len({s.split("::")[0] for s in plan["tests"]}), 6)
        self.assertIn("tests/test_changed.py", plan["tests"])
        self.assertTrue(any(s.startswith("tests/test_consumer_9.py") for s in plan["tests"]))
        self.assertTrue(plan["deferred_tests"])
        self.assertFalse(any("not slow" in c["argv"] for c in plan["commands"]))

    def test_explicit_mapping_covers_script_outside_package(self) -> None:
        self.write("outside/tool.py", "tool = 1\n")
        self.module.CFG["groups"] = [
            {"name": "tools", "files": ["outside/*.py"], "tests": ["tests/test_leaf.py"]}
        ]
        plan = self.module.plan(self.args(), self.module.paths(), ["outside/tool.py"])
        self.assertFalse(plan["manual_required"])
        self.assertIn("tests/test_leaf.py", plan["tests"])

    def test_matching_success_is_reused(self) -> None:
        first = self.execute()
        second = self.execute()
        self.assertEqual(first["status"], "passed")
        self.assertIn("reused_from", second)
        self.assertEqual(second["steps"], [])

    def test_changed_input_invalidates_cache(self) -> None:
        self.execute()
        self.write("app/leaf.py", "value = 3\n")
        self.assertNotIn("reused_from", self.execute())

    def test_changed_git_baseline_invalidates_cache(self) -> None:
        self.execute()
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "--allow-empty",
                "-qm",
                "new baseline",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        self.assertNotIn("reused_from", self.execute())

    def test_failed_receipt_is_not_reused(self) -> None:
        first = self.execute("raise SystemExit(7)")
        second = self.execute("raise SystemExit(7)")
        self.assertEqual(first["status"], "failed")
        self.assertNotIn("reused_from", second)

    def test_modified_log_invalidates_cache(self) -> None:
        first = self.execute()
        Path(first["steps"][0]["log"]).write_text("changed", encoding="utf-8")
        self.assertNotIn("reused_from", self.execute())

    def test_budget_stops_process_and_releases_lock(self) -> None:
        result = self.execute("import time; time.sleep(30)", budget=1)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertFalse((self.root / ".validation/running.lock").exists())
        self.assertTrue(result["deferred_commands"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
