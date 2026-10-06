"""卡4 SWE-bench 导入器测试:零网络。

上游仓库用本地 git 目录充当(`git fetch --depth 1 origin <sha>` 走 file 路径实测可用),
`_repo_url` 被 monkeypatch 成本地路径——真实克隆逻辑、test_patch 应用、纯工作树导出、
基线硬校验与 tasks.json 幂等全部照跑。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from app.evals.bugset import load_bug
from app.gitops.blockpatch import parse_block_patch
from scripts.import_swebench import (
    build_manifest,
    fetch_instance,
    import_instance,
    kept_p2p,
    replay_blocker,
    validate_entry,
)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", check=True
    )
    return proc.stdout.strip()


@pytest.fixture()
def upstream(tmp_path: Path) -> Path:
    """一个两 commit 的小上游:src/mod.py 有缺陷,tests/ 覆盖它。"""
    repo = tmp_path / "upstream"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "mod.py").write_text(
        'def sign(n):\n    """正数 1,负数 -1,零也应返回 0。"""\n    return 1 if n >= 0 else -1\n',
        encoding="utf-8",
        newline="\n",
    )
    (repo / "tests" / "test_ok.py").write_text(
        "from src.mod import sign\n\n\ndef test_positive():\n    assert sign(3) == 1\n",
        encoding="utf-8",
        newline="\n",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


F2P = "tests/test_sign.py::test_zero_returns_zero"
P2P = "tests/test_ok.py::test_positive"

TEST_PATCH = (
    "diff --git a/tests/test_sign.py b/tests/test_sign.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/tests/test_sign.py\n"
    "@@ -0,0 +1,5 @@\n"
    "+from src.mod import sign\n"
    "+\n"
    "+\n"
    "+def test_zero_returns_zero():\n"
    "+    assert sign(0) == 0\n"
)

GOLD_PATCH = (
    "diff --git a/src/mod.py b/src/mod.py\n"
    "--- a/src/mod.py\n"
    "+++ b/src/mod.py\n"
    "@@ -1,3 +1,3 @@\n"
    " def sign(n):\n"
    '     """正数 1,负数 -1,零也应返回 0。"""\n'
    "-    return 1 if n >= 0 else -1\n"
    "+    if n == 0:\n"
    "+        return 0\n"
    "+    return 1 if n > 0 else -1\n"
)


def _instance(upstream: Path, sha: str, **overrides):
    from app.evals.swebench import SweInstance

    values = {
        "instance_id": "pkg__mod-1",
        "repo": "fake/pkg",
        "base_commit": sha,
        "problem_statement": "sign(0) 返回 1,应为 0",
        "fail_to_pass": [F2P],
        "pass_to_pass": [P2P],
        "patch": GOLD_PATCH,
        "test_patch": TEST_PATCH,
    }
    values.update(overrides)
    return SweInstance(**values)


@pytest.fixture()
def offline_clone(monkeypatch, upstream: Path) -> Path:
    """把克隆目标从 GitHub 换成本地目录(离线可用)。"""
    import scripts.import_swebench as mod

    monkeypatch.setattr(mod, "_repo_url", lambda instance: str(upstream).replace("\\", "/"))
    return upstream


def test_fetch_instance_checks_out_base_and_applies_test_patch(
    offline_clone: Path, upstream: Path, tmp_path: Path
) -> None:
    sha = _git(upstream, "rev-parse", "HEAD")
    checkout = fetch_instance(_instance(upstream, sha), tmp_path / "cache")
    assert (checkout / ".git").is_dir()
    assert _git(checkout, "rev-parse", "HEAD") == sha
    assert (checkout / "tests" / "test_sign.py").exists()  # test_patch 已应用
    # 缓存复用:第二次不再联网,直接命中
    again = fetch_instance(_instance(upstream, sha), tmp_path / "cache")
    assert again == checkout


def test_fetch_instance_requires_test_patch(offline_clone: Path, tmp_path: Path) -> None:
    sha = _git(offline_clone, "rev-parse", "HEAD")
    inst = _instance(offline_clone, sha, test_patch="")
    with pytest.raises(RuntimeError, match="test_patch"):
        fetch_instance(inst, tmp_path / "cache")


def test_import_instance_writes_bug_shaped_directory(offline_clone: Path, tmp_path: Path) -> None:
    sha = _git(offline_clone, "rev-parse", "HEAD")
    inst = _instance(offline_clone, sha)
    bugs_root = tmp_path / "bugs"
    status, reason, entry = import_instance(inst, bugs_root, tmp_path / "cache", max_p2p=50)
    assert status == "ok", reason

    bug_dir = bugs_root / f"SWE-{inst.instance_id}"
    assert (bug_dir / "issue.md").read_text(encoding="utf-8").strip() == inst.problem_statement
    assert not (bug_dir / "repo" / ".git").exists()  # 纯工作树
    assert (bug_dir / "expected" / "reference.diff").read_text(encoding="utf-8") == GOLD_PATCH

    manifest = yaml.safe_load((bug_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["id"] == f"SWE-{inst.instance_id}"
    assert manifest["failed_tests"] == [F2P]
    assert manifest["regression_tests"] == [P2P]
    assert manifest["category"] == "swe-bench"
    assert manifest["difficulty"] == "external"
    assert manifest["allowed_paths"] == []
    assert manifest["max_rounds"] == 5

    bug = load_bug(bug_dir, bugs_root)  # 与自建题同一载入路径
    assert bug.failed_tests == [F2P] and bug.regression_tests == [P2P]

    replay = json.loads((bug_dir / "replay" / "script.json").read_text(encoding="utf-8"))
    step = next(s for s in replay if s["tool"] == "apply_patch")
    assert set(step["args"]) == {"patch_text"}
    assert step["args"]["patch_text"].startswith("*** Begin Patch")
    sections = parse_block_patch(step["args"]["patch_text"])
    assert [s.path for s in sections] == ["src/mod.py"]
    graph = json.loads((bug_dir / "replay" / "graph-script.json").read_text(encoding="utf-8"))
    # 定位段不得出现写工具(graph 引擎按阶段拦截)
    assert all(s["tool"] != "apply_patch" for s in graph[: graph.index(step)])
    assert entry["replay"] is True and entry["p2p_kept"] == 1


def test_cli_import_is_idempotent(tmp_path: Path, upstream: Path, monkeypatch) -> None:
    """跑两次 CLI:题目重建、tasks.json 按 id 合并去重(不追加重复条目)。"""
    import scripts.import_swebench as mod

    sha = _git(upstream, "rev-parse", "HEAD")
    monkeypatch.setattr(mod, "_repo_url", lambda instance: str(upstream).replace("\\", "/"))
    jsonl = tmp_path / "swe10.jsonl"
    record = {
        "instance_id": "pkg__mod-1",
        "repo": "fake/pkg",
        "base_commit": sha,
        "problem_statement": "sign(0) 返回 1,应为 0",
        "FAIL_TO_PASS": json.dumps([F2P]),
        "PASS_TO_PASS": json.dumps([P2P]),
        "patch": GOLD_PATCH,
        "test_patch": TEST_PATCH,
    }
    jsonl.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    bugs_root = tmp_path / "bugs"
    argv = [
        "--jsonl",
        str(jsonl),
        "--bugs-root",
        str(bugs_root),
        "--cache",
        str(tmp_path / "cache"),
        "--ids",
        "pkg__mod-1",
    ]
    assert mod.main(argv) == 0
    assert mod.main(argv) == 0

    tasks = json.loads((tmp_path / "eval_suite" / "tasks.json").read_text(encoding="utf-8"))
    assert len(tasks) == 1, tasks
    assert tasks[0]["id"] == "SWE-pkg__mod-1"
    assert tasks[0]["replay"] is True and tasks[0]["p2p_kept"] == 1
    assert tasks[0]["base_commit"] == sha
    assert (bugs_root / "SWE-pkg__mod-1" / "repo" / "src" / "mod.py").is_file()


def test_validate_entry_rejects_already_green_baseline(offline_clone: Path, tmp_path: Path) -> None:
    """基线已绿的题必须被拒:它的 FAIL_TO_PASS 无从证明"修好了"。"""
    sha = _git(offline_clone, "rev-parse", "HEAD")
    bugs_root = tmp_path / "bugs"
    bug_dir = bugs_root / "SWE-already-green"
    (bug_dir / "repo").mkdir(parents=True)
    import scripts.import_swebench as mod

    mod.export_repo(offline_clone, bug_dir / "repo")
    (bug_dir / "issue.md").write_text("x\n", encoding="utf-8")
    inst = _instance(offline_clone, sha, instance_id="already-green", fail_to_pass=[P2P])
    (bug_dir / "manifest.yaml").write_text(
        build_manifest(inst, [P2P]), encoding="utf-8", newline="\n"
    )
    reasons = validate_entry(bug_dir)
    assert reasons, "基线已绿的条目应当被拒"
    assert "已全绿" in reasons[0]


def test_validate_entry_requires_green_canary(offline_clone: Path, tmp_path: Path) -> None:
    """PASS_TO_PASS 为空的条目一律"不可证":环境缺依赖时 F2P 也以收集错误失败,
    没有必然为绿的探针就分不开"题目没修"与"这台机器跑不了这个 repo"。
    """
    sha = _git(offline_clone, "rev-parse", "HEAD")
    bug_dir = tmp_path / "bugs" / "SWE-no-p2p"
    bug_dir.mkdir(parents=True)
    import scripts.import_swebench as mod

    mod.export_repo(offline_clone, bug_dir / "repo")
    (bug_dir / "issue.md").write_text("x\n", encoding="utf-8")
    inst = _instance(offline_clone, sha, instance_id="no-p2p", pass_to_pass=[])
    (bug_dir / "manifest.yaml").write_text(build_manifest(inst, []), encoding="utf-8", newline="\n")
    reasons = validate_entry(bug_dir)
    assert reasons and "绿灯探针" in reasons[0]


def test_validate_entry_rejects_dirty_regression_set(offline_clone: Path, tmp_path: Path) -> None:
    sha = _git(offline_clone, "rev-parse", "HEAD")
    bugs_root = tmp_path / "bugs"
    bug_dir = bugs_root / "SWE-dirty-p2p"
    bug_dir.mkdir(parents=True)
    import scripts.import_swebench as mod

    mod.export_repo(offline_clone, bug_dir / "repo")
    (bug_dir / "repo" / "src" / "mod.py").write_text(
        'def sign(n):\n    raise RuntimeError("boom")\n', encoding="utf-8", newline="\n"
    )
    (bug_dir / "issue.md").write_text("x\n", encoding="utf-8")
    inst = _instance(
        offline_clone, sha, instance_id="dirty-p2p", fail_to_pass=[F2P], pass_to_pass=[P2P]
    )
    (bug_dir / "manifest.yaml").write_text(
        build_manifest(inst, [P2P]), encoding="utf-8", newline="\n"
    )
    reasons = validate_entry(bug_dir)
    assert any("PASS_TO_PASS" in r for r in reasons), reasons


def test_replay_blocker_flags_gold_patch_touching_tests(offline_clone: Path) -> None:
    """gold patch 改测试文件 → 回放必然被 files 门禁拒 → 只出真实模型条目。"""
    sha = _git(offline_clone, "rev-parse", "HEAD")
    inst = _instance(
        offline_clone,
        sha,
        patch=GOLD_PATCH
        + (
            "diff --git a/tests/test_sign.py b/tests/test_sign.py\n"
            "--- a/tests/test_sign.py\n"
            "+++ b/tests/test_sign.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
        ),
    )
    assert "测试文件" in replay_blocker(inst)
    no_patch = _instance(offline_clone, sha, patch="")
    assert replay_blocker(no_patch) == "缺 patch"


def test_kept_p2p_is_deterministic_and_bounded() -> None:
    sha = "0123456789abcdef"
    inst = _instance(Path("."), sha, pass_to_pass=[f"tests/t.py::test_{i}" for i in range(120)])
    first = kept_p2p(inst, 50)
    second = kept_p2p(inst, 50)
    assert first == second and len(first) == 50
    # 0 = 不截断:洗牌后仍是全量同一集合(顺序按种子固定,不是原序)
    assert set(kept_p2p(inst, 0)) == set(inst.pass_to_pass)
    assert len(kept_p2p(inst, 0)) == 120


def test_repo_url_guard_rejects_hostile_values() -> None:
    import scripts.import_swebench as mod

    sha = "0123456789abcdef0123456789abcdef01234567"
    for hostile in ("evil/../../x", "a/b c", ""):
        with pytest.raises(RuntimeError, match="repo"):
            mod._repo_url(_instance(Path("."), sha, repo=hostile))
    with pytest.raises(RuntimeError, match="base_commit"):
        mod._repo_url(_instance(Path("."), "--upload-pack=/tmp/x"))


def test_legacy_checkouts_mode_still_works(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """旧语义(只校验人工 checkout,不联网)保留:--checkouts-root 路径不变。"""
    import scripts.import_swebench as mod

    sha = "0123456789abcdef0123456789abcdef01234567"
    jsonl = tmp_path / "swe.jsonl"
    record = {
        "instance_id": "a--b-1",
        "repo": "acme/thing",
        "base_commit": sha,
        "problem_statement": "x",
        "FAIL_TO_PASS": json.dumps(["tests/t.py::test_a"]),
        "PASS_TO_PASS": json.dumps(["tests/t.py::test_b"]),
    }
    jsonl.write_text(json.dumps(record) + "\n", encoding="utf-8")
    rc = mod.main(["--jsonl", str(jsonl), "--checkouts-root", str(tmp_path / "none")])
    assert rc == 1  # checkout 缺失
    assert "1 missing" in capsys.readouterr().out


def test_parse_instance_reads_optional_patch_fields() -> None:
    from app.evals.swebench import parse_instance

    inst = parse_instance(
        {
            "instance_id": "x-1",
            "repo": "acme/thing",
            "base_commit": "abc",
            "problem_statement": "p",
            "FAIL_TO_PASS": '["t::a"]',
            "PASS_TO_PASS": "[]",
            "patch": GOLD_PATCH,
            "test_patch": TEST_PATCH,
        }
    )
    assert inst.patch == GOLD_PATCH and inst.test_patch == TEST_PATCH
    bare = parse_instance(
        {
            "instance_id": "x-2",
            "repo": "acme/thing",
            "base_commit": "abc",
            "problem_statement": "p",
            "FAIL_TO_PASS": '["t::a"]',
            "PASS_TO_PASS": "[]",
        }
    )
    assert bare.patch == "" and bare.test_patch == ""
