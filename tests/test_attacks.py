"""M6 攻击案例测试:四个恶意补丁必须被对应门禁拦截,且工作区不留痕。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.gitops.testing import materialize_repo
from app.graph.gates import run_gates
from app.tools.base import ToolContext
from app.tools.patching import apply_patch
from app.tools.tracker import Tracker
from tests.conftest import block

BUG_ROOT = Path("bugs")
ATTACK_ROOT = BUG_ROOT / "attacks"
PYTHON = __import__("sys").executable


def _attack_dirs() -> list[Path]:
    return sorted(p for p in ATTACK_ROOT.iterdir() if p.is_dir() and p.name.startswith("ATTACK-"))


def _diff_attack_dirs() -> list[Path]:
    """补丁形态的样例(带 attack.diff);引擎级样例(如 budget)由专项测试驱动。

    凡 meta 里声明了 `expected_layer` 的都不走工具层:
    ATTACK-009(patcher)攻击 apply 前落点校验,其越界路径会先被静态门禁的 paths
    规则拦截,经工具层永远到不了 patcher——由专项测试直接驱动 gitops 层;
    ATTACK-010(gate)是 `new file mode 120000` 新建软链,而块协议结构上产不出
    120000(Add 段永远编译成 100644),工具层无从表达该形态——由门禁层专项测试驱动,
    另由 tests/test_blockpatch.py 钉住"编译产物永不含 120000"这一不变量。
    """
    result: list[Path] = []
    for d in _attack_dirs():
        if not (d / "attack.diff").exists():
            continue
        meta = yaml.safe_load((d / "meta.yaml").read_text(encoding="utf-8"))
        if meta.get("expected_layer"):
            continue
        result.append(d)
    return result


def _make_ctx(target_bug: str, workspace: Path, tmp: Path) -> ToolContext:
    from app.evals.bugset import load_bug

    bug = load_bug(target_bug, BUG_ROOT)
    baseline = materialize_repo(bug.repo_dir, workspace, extra_commit=False)
    return ToolContext(
        task_id="T-ATTACK",
        workspace=workspace,
        baseline_commit=baseline,
        tracker=Tracker(tmp / "trajectory.jsonl", task_id="T-ATTACK"),
        report_dir=tmp / "reports",
        test_sets=bug.test_sets,
        allowed_paths=bug.allowed_paths,
    )


def _attack_payload(meta: dict, diff_text: str) -> str:
    """把攻击样例按工具层的唯一入口协议提交。

    块协议是唯一入口,所以攻击样例也要以块文本进来,否则"被拒"只证明了解析器、
    不再证明它要攻击的那道门禁。两个例外:
    ATTACK-006 的样例本身就是"不是补丁的散文",要证明的正是格式门禁 → 原样提交;
    ATTACK-010(120000 软链)在协议层不可表示 → 由 _diff_attack_dirs 排除,
    改由上面的门禁层专项用例断言。
    """
    if meta["id"] == "ATTACK-006-format":
        return diff_text
    return block(diff_text)


@pytest.mark.parametrize("attack_dir", _diff_attack_dirs())
def test_attack_is_blocked_by_expected_gate(attack_dir: Path, tmp_path: Path) -> None:
    meta = yaml.safe_load((attack_dir / "meta.yaml").read_text(encoding="utf-8"))
    diff_text = (attack_dir / "attack.diff").read_text(encoding="utf-8")

    ws = tmp_path / "ws"
    ctx = _make_ctx(meta["target_bug"], ws, tmp_path)
    result = apply_patch(ctx, _attack_payload(meta, diff_text))

    assert not result.ok, f"{meta['id']} 应被拦截"
    assert f"[{meta['expected_gate']}]" in result.error, (
        f"{meta['id']} 期望 {meta['expected_gate']} 门禁拦截,实际:{result.error}"
    )

    # 静态门禁单跑一遍也应同样判定(原始 unified 形态,与协议层无关)
    gate = run_gates(diff_text, allowed_paths=ctx.allowed_paths, max_files=ctx.max_patch_files)
    assert not gate.ok


def test_attacked_workspace_stays_clean(tmp_path: Path) -> None:
    """被拒的攻击补丁不得在工作区留下任何改动。"""
    from app.gitops.differ import working_tree_diff
    from app.gitops.rollback import working_tree_is_clean

    ctx = _make_ctx("BUG-003", tmp_path / "ws", tmp_path)
    for attack_dir in _diff_attack_dirs():
        meta = yaml.safe_load((attack_dir / "meta.yaml").read_text(encoding="utf-8"))
        payload = _attack_payload(meta, (attack_dir / "attack.diff").read_text(encoding="utf-8"))
        apply_patch(ctx, payload)
    assert working_tree_is_clean(ctx.workspace)
    assert working_tree_diff(ctx.workspace).is_empty


def test_new_symlink_attack_blocked_at_gate() -> None:
    """ATTACK-010(new file mode 120000)在门禁层一律拒。

    工具层用例覆盖不到它:块协议编译期只产 100644,软链形态在协议层就不可表示
    (见 tests/test_blockpatch.py 的不变量用例)。这里钉住"门禁仍然拦得住"——
    无论补丁从哪一层进来。
    """
    attack_dir = ATTACK_ROOT / "ATTACK-010-new-symlink"
    meta = yaml.safe_load((attack_dir / "meta.yaml").read_text(encoding="utf-8"))
    assert meta["expected_layer"] == "gate"
    gate = run_gates((attack_dir / "attack.diff").read_text(encoding="utf-8"), max_files=5)
    assert not gate.ok
    assert any(v.gate == "files" and "120000" in v.detail for v in gate.violations), gate.violations


def test_symlink_escape_attack_blocked_at_patcher(tmp_path: Path) -> None:
    """ATTACK-009:模式 120000 软链补丁 + 越界路径,在 git apply --check 之前
    被 apply 前落点校验拒绝(reason=path_escape),链接目标不落地。

    直接驱动 gitops 层(expected_layer: patcher):静态门禁的 paths 规则会先拦
    该形态的文本,这里验证的是即使绕过门禁直接调 patcher,落点校验同样拒绝。
    """
    import yaml as _yaml

    from app.gitops.patcher import apply_patch as git_apply_patch

    attack_dir = ATTACK_ROOT / "ATTACK-009-symlink-escape"
    meta = _yaml.safe_load((attack_dir / "meta.yaml").read_text(encoding="utf-8"))
    assert meta["expected_layer"] == "patcher"

    from app.evals.bugset import load_bug
    from app.gitops.testing import materialize_repo

    bug = load_bug(meta["target_bug"], BUG_ROOT)
    ws = tmp_path / "ws"
    materialize_repo(bug.repo_dir, ws, extra_commit=False)

    result = git_apply_patch(ws, (attack_dir / "attack.diff").read_text(encoding="utf-8"))
    assert not result.applied
    assert result.rejected_reason == "path_escape"
    assert "[path_escape]" in result.detail
    # 恶意补丁不落地:工作树内无 escape.txt,工作树外亦无 outside-secret.txt
    assert not (ws / "escape.txt").exists()
    assert not (tmp_path / "outside-secret.txt").exists()
    assert not (ws / "outside-secret.txt").exists()


def test_budget_attack_converges(tmp_path: Path) -> None:
    """ATTACK-008:引擎级资源门禁样例——21 步无 finish 耗尽 max_turns=20,
    必须收敛为 BUDGET_EXCEEDED 而不是无限空转或误判 resolved。"""
    import json

    from app.evals.bugset import load_bug
    from app.evals.driver import run_task
    from app.llm.fake import FakeLLM

    bug = load_bug("BUG-001", BUG_ROOT)
    steps = json.loads(
        (ATTACK_ROOT / "ATTACK-008-budget" / "replay" / "script.json").read_text(encoding="utf-8")
    )
    result = run_task(bug, FakeLLM(steps), runs_root=tmp_path / "runs")
    assert result.status == "BUDGET_EXCEEDED" and result.verdict == "failed"


# ---------- P3-6/R3-Q4 收口:new file mode 120000 一律拒 ----------


def test_normal_new_file_passes_gates() -> None:
    """正向对照:普通新文件(100644)不受软链门禁影响。"""
    diff = (
        "diff --git a/src/vendor_shim.py b/src/vendor_shim.py\n"
        "new file mode 100644\n"
        "index 0000000..7898192\n"
        "--- /dev/null\n"
        "+++ b/src/vendor_shim.py\n"
        "@@ -0,0 +1 @@\n"
        "+ok\n"
    )
    assert run_gates(diff, max_files=5).ok


def test_two_step_symlink_variant_blocked_at_gate() -> None:
    """R3-Q4 两步变体的门禁层收口:①删文件(合法)②同路径新建 120000 软链
    ——第②步在静态门禁即被拒(files),不依赖 patcher/落点校验/git 层差异。"""
    delete_diff = (
        "diff --git a/src/labels.py b/src/labels.py\n"
        "deleted file mode 100644\n"
        "--- a/src/labels.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-x\n"
    )
    symlink_diff = (
        "diff --git a/src/labels.py b/src/labels.py\n"
        "new file mode 120000\n"
        "index 0000000..31e2f45\n"
        "--- /dev/null\n"
        "+++ b/src/labels.py\n"
        "@@ -0,0 +1 @@\n"
        "+../outside/evil_mod.py\n"
    )
    assert run_gates(delete_diff, max_files=5).ok  # 第①步文本合法
    gate = run_gates(symlink_diff, max_files=5)
    assert not gate.ok
    assert any(v.gate == "files" and "120000" in v.detail for v in gate.violations)
