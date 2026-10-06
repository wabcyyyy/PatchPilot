"""SWE-bench 题目自动导入器(一次性运维脚本:允许联网,人工触发,不进 CI)。

两种模式:
    # ① 校验已有人工 checkout(旧语义,保留)
    python scripts/import_swebench.py --jsonl data.jsonl --checkouts-root ./checkouts
    # ② 自动导入:克隆 base_commit + 应用 test_patch + 落盘成 bugs/SWE-xxx 题目
    python scripts/import_swebench.py --jsonl data/swe10.jsonl --ids a,b --bugs-root bugs --cache .swe_cache

模式 ② 的落盘形态与自建题完全同构(load_bug 可直接载入):

    bugs/SWE-<instance_id>/
      manifest.yaml             failed_tests=FAIL_TO_PASS / regression_tests=P2P 截断
      issue.md                  problem_statement
      repo/                     base_commit + test_patch 之后的纯工作树(无 .git)
      expected/reference.diff   gold patch(unified)
      replay/{script,graph-script}.json   由 gold patch 机械生成的回放
    eval_suite/tasks.json       幂等索引(存在则合并去重)

**基线硬校验**:落盘前用真实 pytest 跑一遍——FAIL_TO_PASS 必须真的失败、
保留的 PASS_TO_PASS 必须真的绿;不满足就打印原因、删目录跳过(题面不成立的题目
留在语料里只会把评测数字变成噪声)。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.adapters.pytest_adapter import run_pytest  # noqa: E402
from app.evals.bugset import load_bug, validate_test_ids  # noqa: E402
from app.evals.swebench import SweInstance, load_instances  # noqa: E402
from app.gitops.blockpatch import (  # noqa: E402
    BlockPatchError,
    parse_block_patch,
    unified_to_block,
)
from app.gitops.patcher import apply_patch as git_apply_patch  # noqa: E402
from app.graph.gates import parse_diff_files  # noqa: E402
from app.tools.paths import is_test_file, normalize_rel  # noqa: E402

# 数据集里的 repo 形如 "astropy/astropy";它是外部输入且会进 git 命令行,
# 必须先过白名单再拼接(否则 `--upload-pack=...` 之类形态直达 argv)。
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
# 导出纯工作树时忽略的目录(它们既大又与题面无干)
_EXPORT_IGNORES = {".git", "__pycache__", ".pytest_cache", ".tox", "node_modules", ".venv"}


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 900) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _repo_url(instance: SweInstance) -> str:
    if not _REPO_SLUG_RE.match(instance.repo):
        raise RuntimeError(f"{instance.instance_id}: 非法 repo 字段 {instance.repo!r}")
    if not _SHA_RE.match(instance.base_commit):
        raise RuntimeError(f"{instance.instance_id}: 非法 base_commit {instance.base_commit!r}")
    return f"https://github.com/{instance.repo}.git"


def fetch_instance(instance: SweInstance, cache_dir: Path) -> Path:
    """取 base_commit 的检出并应用 test_patch;缓存目录可复用(幂等)。"""
    url = _repo_url(instance)
    name = instance.repo.split("/")[-1]
    work = cache_dir / f"{name}-{instance.base_commit[:12]}"
    marker = work / ".patchpilot-test-patch-applied"
    if (work / ".git").is_dir():
        rc, head, _ = _run(["git", "rev-parse", "HEAD"], cwd=work)
        if rc == 0 and head.strip().startswith(instance.base_commit[:12]) and marker.exists():
            print(f"[cache] {instance.instance_id}: 复用 {work.name}")
            return work
        _run(["git", "clean", "-xfd"], cwd=work)
    else:
        work.mkdir(parents=True, exist_ok=True)
        _run(["git", "init", "-q"], cwd=work)
        _run(["git", "remote", "add", "origin", "--", url], cwd=work)
    rc, _, err = _run(
        ["git", "fetch", "-q", "--depth", "1", "origin", instance.base_commit],
        cwd=work,
        timeout=1800,
    )
    if rc != 0:
        # 服务端不支持按 sha 直取(fetch --depth 1 <sha> 需要 allowAnySHA1InWant):
        # 退化为常规分支克隆后再检出
        rc2, _, err2 = _run(["git", "fetch", "-q", "origin", "HEAD"], cwd=work, timeout=3600)
        if rc2 != 0:
            raise RuntimeError(f"{instance.instance_id}: fetch 失败:{err.strip() or err2.strip()}")
    rc, _, err = _run(["git", "checkout", "-f", "-q", instance.base_commit], cwd=work)
    if rc != 0:
        raise RuntimeError(f"{instance.instance_id}: checkout 失败:{err.strip()}")
    if not instance.test_patch.strip():
        raise RuntimeError(f"{instance.instance_id}: 缺 test_patch,基线无从建立")
    applied = git_apply_patch(work, instance.test_patch)
    if not applied.applied:
        raise RuntimeError(
            f"{instance.instance_id}: test_patch 应用失败"
            f"({applied.rejected_reason}):{applied.detail.splitlines()[:2]}"
        )
    marker.write_text(instance.instance_id, encoding="utf-8")
    return work


def _copy_working_tree(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        if entry.name in _EXPORT_IGNORES or entry.name == ".patchpilot-test-patch-applied":
            continue
        target = dest / entry.name
        if entry.is_dir():
            _copy_working_tree(entry, target)
        elif entry.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)


def export_repo(checkout: Path, dest: Path) -> None:
    """剥 .git 的纯工作树拷贝:题目 repo/ 与被测代码一起进语料。"""
    if dest.exists():
        shutil.rmtree(dest)
    _copy_working_tree(checkout, dest)


def kept_p2p(instance: SweInstance, max_p2p: int) -> list[str]:
    """PASS_TO_PASS 以 instance_id 为固定种子洗牌后截断(可复现,又不总取同一前缀)。"""
    pool = list(instance.pass_to_pass)
    random.Random(instance.instance_id).shuffle(pool)
    return pool[:max_p2p] if max_p2p > 0 else pool


def build_manifest(instance: SweInstance, p2p_kept: list[str], bug_id: str | None = None) -> str:
    # manifest 的 id 必须等于题目目录名(load_bug 以 manifest.id 为任务身份)
    bid = bug_id or f"SWE-{instance.instance_id}"

    def _block(key: str, values: list[str]) -> list[str]:
        if not values:
            return [f"{key}: []"]
        return [f"{key}:", *[f"  - {value}" for value in values]]

    lines = [
        f"id: {bid}",
        "category: swe-bench",
        "difficulty: external",
        'test_cmd: "{python} -m pytest"',
        *_block("failed_tests", instance.fail_to_pass),
        *_block("regression_tests", p2p_kept),
        "allowed_paths: []",  # 外部仓库无修改范围先验;门禁仍拦测试文件与越界路径
        "max_rounds: 5",
        "",
    ]
    return "\n".join(lines)


def replay_blocker(instance: SweInstance) -> str:
    """gold patch 能否作为回放补丁存在(块协议 + 门禁口径)。

    SWE-bench 的 gold patch 常连测试一起改——回放它必然被 `files` 门禁拒(那是门禁
    正确,不是回放坏);越界路径同理。这类题只出真实模型条目(replay:false),
    不产出会被误读成"协议坏了"的坏回放。
    """
    if not instance.patch.strip():
        return "缺 patch"
    try:
        sections = parse_block_patch(unified_to_block(instance.patch))
    except BlockPatchError as exc:
        return f"块转换失败 [{exc.tag}] {exc.reason}"
    for rel in parse_diff_files(instance.patch):
        norm = normalize_rel(rel)
        if is_test_file(norm):
            return f"gold patch 触及测试文件 {norm}"
        parts = norm.split("/")
        if ".." in parts or ".git" in parts or norm.startswith(("/", "\\")):
            return f"gold patch 路径越界 {norm}"
    for section in sections:
        if section.action == "add" and any(ln.startswith("-") for ln in section.lines):
            return f"新增段含删除行 {section.path}"
    return ""


def build_replay_steps(
    instance: SweInstance, block_text: str, p2p_kept: list[str]
) -> tuple[list[dict], list[dict]]:
    """由 gold patch 机械生成回放:(plain 脚本, graph 脚本)。"""
    sources = [
        rel
        for rel in parse_diff_files(instance.patch)
        if rel.endswith(".py") and not is_test_file(rel)
    ]
    apply_steps = [
        {"tool": "apply_patch", "args": {"patch_text": block_text}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
    ]
    # 回归集为空的题不排 run_tests(regression):那会撞上 "test_set is empty" 而错位
    if p2p_kept:
        apply_steps.append({"tool": "run_tests", "args": {"test_set": "regression"}})
    apply_steps.append(
        {
            "tool": "finish",
            "args": {
                "success": True,
                "summary": f"按 gold patch 修复 {instance.instance_id} 并通过双测试集",
            },
        }
    )
    plain = [{"tool": "read_file", "args": {"path": p}} for p in sources] + apply_steps
    localize = [{"tool": "read_file", "args": {"path": p}} for p in sources] + [
        {
            "tool": "finish",
            "args": {"success": True, "summary": instance.problem_statement.strip()[:120]},
        }
    ]
    return plain, localize + apply_steps


def validate_entry(bug_dir: Path) -> list[str]:
    """基线硬校验:题面能载入、FAIL_TO_PASS 真失败、保留的 PASS_TO_PASS 真绿。

    PASS_TO_PASS 至少留一条作绿灯探针:环境缺依赖时 F2P 也会以收集错误"失败",
    没有必然为绿的对照用例就无从区分"题目本身没修"与"这台机器跑不了这个 repo"。
    """
    import tempfile

    reasons: list[str] = []
    try:
        bug = load_bug(bug_dir, bug_dir.parent)
    except Exception as exc:  # bugset 用 TaskError,这里只需把它转成跳过原因
        return [f"load_bug 失败:{exc}"]
    try:
        validate_test_ids(bug.failed_tests, bug.id)
        validate_test_ids(bug.regression_tests, bug.id)
    except Exception as exc:
        reasons.append(f"测试 id 不合平台白名单口径:{exc}")
        return reasons
    if not bug.regression_tests:
        return [
            "基线不可证:PASS_TO_PASS 为空,没有绿灯探针"
            "(环境缺依赖时 F2P 同样以收集错误失败,无从与真实缺陷区分)"
        ]

    tmp = Path(tempfile.mkdtemp(prefix="swe-validate-"))
    try:
        failed_report, _ = run_pytest(
            sys.executable, bug.repo_dir, bug.failed_tests, tmp / "f2p.xml", timeout_seconds=600
        )
        if failed_report.all_passed:
            reasons.append("基线不成立:FAIL_TO_PASS 在未修复的 repo 上已全绿")
        elif not failed_report.failed_cases:
            reasons.append("基线不成立:FAIL_TO_PASS 没有真实失败用例(收集错误?)")
        if bug.regression_tests:
            regression_report, _ = run_pytest(
                sys.executable,
                bug.repo_dir,
                bug.regression_tests,
                tmp / "p2p.xml",
                timeout_seconds=600,
            )
            if not regression_report.all_passed:
                bad = ", ".join(c.signature[:60] for c in regression_report.failed_cases[:3])
                reasons.append(f"基线不成立:保留的 PASS_TO_PASS 不绿({bad})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return reasons


def _update_tasks_index(tasks_path: Path, entries: list[dict]) -> None:
    """幂等索引:按 id 合并去重,保留更早的 imported_at 之外的字段覆盖。"""
    existing: list[dict] = []
    if tasks_path.exists():
        try:
            loaded = json.loads(tasks_path.read_text(encoding="utf-8"))
            existing = loaded if isinstance(loaded, list) else loaded.get("tasks", [])
        except json.JSONDecodeError:
            existing = []
    by_id = {str(item.get("id")): item for item in existing}
    for entry in entries:
        by_id[entry["id"]] = entry
    merged = [by_id[key] for key in sorted(by_id)]
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    tasks_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def import_instance(
    instance: SweInstance,
    bugs_root: Path,
    cache_dir: Path,
    max_p2p: int,
    *,
    skip_validate: bool = False,
) -> tuple[str, str, dict | None]:
    """导入一题,返回 (状态, 原因, tasks 条目)。状态 ∈ {ok, replay-only, skipped}。"""
    bug_id = f"SWE-{instance.instance_id}"
    bug_dir = bugs_root / bug_id
    p2p_kept = kept_p2p(instance, max_p2p)
    try:
        checkout = fetch_instance(instance, cache_dir)
    except RuntimeError as exc:
        return "skipped", str(exc), None

    bug_dir.mkdir(parents=True, exist_ok=True)
    (bug_dir / "issue.md").write_text(instance.problem_statement.strip() + "\n", encoding="utf-8")
    (bug_dir / "manifest.yaml").write_text(
        build_manifest(instance, p2p_kept), encoding="utf-8", newline="\n"
    )
    export_repo(checkout, bug_dir / "repo")

    blocker = replay_blocker(instance)
    if blocker:
        print(f"[replay-skip] {bug_id}: {blocker}(仍产出条目,仅真实模型可跑)")
    else:
        block_text = unified_to_block(instance.patch)
        (bug_dir / "expected").mkdir(parents=True, exist_ok=True)
        (bug_dir / "expected" / "reference.diff").write_text(instance.patch, encoding="utf-8")
        plain, graph = build_replay_steps(instance, block_text, p2p_kept)
        replay = bug_dir / "replay"
        replay.mkdir(parents=True, exist_ok=True)
        for name, steps in (("script.json", plain), ("graph-script.json", graph)):
            (replay / name).write_text(
                json.dumps(steps, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
            )

    reasons = [] if skip_validate else validate_entry(bug_dir)
    if reasons:
        shutil.rmtree(bug_dir, ignore_errors=True)
        return "skipped", f"{bug_id}: " + ";".join(reasons), None

    entry = {
        "id": bug_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "origin_jsonl": None,  # 由调用方填(可核对数据来源)
        "replay": not blocker,
        "p2p_total": len(instance.pass_to_pass),
        "p2p_kept": len(p2p_kept),
        "imported_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    return ("ok" if not blocker else "replay-only"), "", entry


def check_local_checkouts(instances: list[SweInstance], root: Path) -> int:
    """旧语义:只校验人工备好的 checkout,不联网。"""

    def _git_head(repo_dir: Path) -> str | None:
        try:
            rc, out, _ = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.strip() if rc == 0 else None

    ready, missing = [], []
    for instance in instances:
        if not re.fullmatch(r"[\w.-]+", instance.instance_id):
            raise RuntimeError(f"unsafe instance_id (path guard): {instance.instance_id!r}")
        repo_dir = root / instance.instance_id
        if not repo_dir.is_dir():
            missing.append(f"{instance.instance_id}: local checkout not found: {repo_dir}")
            continue
        head = _git_head(repo_dir)
        if head is None:
            print(f"[warn] {instance.instance_id}: checkout 不是 git 仓库,无法核对 base_commit")
        elif instance.base_commit and head[:12] != instance.base_commit[:12]:
            print(
                f"[warn] {instance.instance_id}: checkout HEAD {head[:8]} != 数据集 base_commit"
                f" {instance.base_commit[:8]}(FAIL_TO_PASS 预期可能不成立)"
            )
        ready.append(instance.instance_id)
        print(
            f"[ready] {instance.instance_id}: failed={len(instance.fail_to_pass)}"
            f" pass_to_pass={len(instance.pass_to_pass)}"
            f" repo={instance.repo}@{instance.base_commit[:8]}"
        )
    for problem in missing:
        print(f"[missing] {problem}")
    print(f"\n{len(ready)} ready / {len(missing)} missing (total {len(instances)})")
    return 0 if not missing else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SWE-bench 数据接入(校验 / 自动导入)")
    parser.add_argument("--jsonl", required=True, help="SWE-bench 格式的本地 jsonl")
    parser.add_argument(
        "--checkouts-root",
        default="",
        help="实例 checkout 根目录:给了就只做校验(旧语义,不联网)",
    )
    parser.add_argument("--instance", default="", help="校验模式下只处理指定 instance_id")
    parser.add_argument("--bugs-root", default="bugs", help="导入模式落盘的题目根目录")
    parser.add_argument("--cache", default=".swe_cache", help="克隆缓存目录")
    parser.add_argument("--ids", default="", help="导入模式:逗号分隔的 instance_id 清单")
    parser.add_argument("--max-p2p", type=int, default=50, help="PASS_TO_PASS 保留上限(0=不限)")
    parser.add_argument(
        "--skip-validate", action="store_true", help="跳过基线硬校验(仅调试题目形态时用)"
    )
    args = parser.parse_args(argv)

    instances = load_instances(args.jsonl)

    if args.checkouts_root:
        selected = (
            [i for i in instances if i.instance_id == args.instance] if args.instance else instances
        )
        return check_local_checkouts(selected, Path(args.checkouts_root))

    wanted = [t.strip() for t in args.ids.split(",") if t.strip()]
    if wanted:
        instances = [i for i in instances if i.instance_id in wanted]
        missing_ids = [w for w in wanted if w not in {i.instance_id for i in instances}]
        for item in missing_ids:
            print(f"[skipped] jsonl 里没有 {item}", file=sys.stderr)
    bugs_root = Path(args.bugs_root)
    cache_dir = Path(args.cache)
    cache_dir.mkdir(parents=True, exist_ok=True)

    written: list[dict] = []
    ok = replay_only = skipped = 0
    for instance in instances:
        print(f"[fetch] {instance.instance_id} ← {instance.repo}@{instance.base_commit[:8]}")
        status, reason, entry = import_instance(
            instance,
            bugs_root,
            cache_dir,
            args.max_p2p,
            skip_validate=args.skip_validate,
        )
        if entry:
            entry["origin_jsonl"] = str(args.jsonl)
            written.append(entry)
        if status == "ok":
            ok += 1
            print(f"[ok] SWE-{instance.instance_id}")
        elif status == "replay-only":
            replay_only += 1
            print(f"[replay-only] SWE-{instance.instance_id}")
        else:
            skipped += 1
            print(f"[skipped] {reason}", file=sys.stderr)

    if written:
        _update_tasks_index(bugs_root.parent / "eval_suite" / "tasks.json", written)
    print(f"\n导入完成:ok={ok} replay-only={replay_only} skipped={skipped}(total {len(instances)})")
    print(
        "next: python -m app.evals.driver --bugs SWE-xxx,... --out runs/swe-fake --model fake"
        "(fake 冒烟验管线;真实 LLM 批次另行确认预算后人工触发)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
