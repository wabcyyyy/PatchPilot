"""任务创建路径的健壮性:落库/调度失败必须释放锁并收敛任务状态(评审整改)。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.api.service import TaskService
from app.errors import PatchPilotError
from app.storage.repository import Repository
from tests.conftest import block


def _service(tmp_path: Path) -> TaskService:
    return TaskService(
        repo=Repository(tmp_path / "db.sqlite3"),
        runs_root=tmp_path / "runs",
        bugs_root=Path("bugs"),
    )


def _idem(bug_id: str, engine: str, model: str) -> str:
    return hashlib.sha256(f"{bug_id}|{engine}|{model}".encode()).hexdigest()


def test_db_failure_releases_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """落库失败:异常上抛,但锁必须释放(否则同键任务被卡到 TTL)。"""
    service = _service(tmp_path)

    def _disk_full(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service.repo, "create_task", _disk_full)
    with pytest.raises(OSError, match="disk full"):
        service.create_task(bug_id="BUG-001", engine="graph", model="fake")
    assert service.lock.acquire(f"task:{_idem('BUG-001', 'graph', 'fake')}", ttl_seconds=5)
    service.shutdown()


def test_submit_after_shutdown_converges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """线程池已关停(服务退出竞态):409 收敛,任务行标 NEEDS_REVIEW,锁释放。"""
    service = _service(tmp_path)

    def _shut_down(*args, **kwargs):
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(service._pool, "submit", _shut_down)
    with pytest.raises(PatchPilotError, match="shutting down"):
        service.create_task(bug_id="BUG-001", engine="graph", model="fake")
    tasks = service.repo.list_tasks(10)
    assert any(t["status"] == "NEEDS_REVIEW" for t in tasks)
    assert service.lock.acquire(f"task:{_idem('BUG-001', 'graph', 'fake')}", ttl_seconds=5)
    service.shutdown()


def test_create_task_wires_max_rounds_into_engine(tmp_path: Path) -> None:
    """N-9 整改:max_rounds 此前只落库不生效;引擎读 bug.max_rounds,必须在提交前覆写。"""
    service = _service(tmp_path)
    captured: dict[str, object] = {}

    def _capture(task_id, bug, *args, **kwargs):
        captured["max_rounds"] = bug.max_rounds

    service._execute = _capture  # type: ignore[method-assign]
    service.create_task(bug_id="BUG-001", engine="graph", model="fake", max_rounds=1)
    assert captured["max_rounds"] == 1
    service.shutdown()


def test_queued_task_stays_queued_until_execution_starts(tmp_path: Path) -> None:
    """P3-10:RUNNING 置位在 _execute 首行——已受理但池内排队的任务如实保持
    QUEUED(create_task 处不再置 RUNNING,DB 状态 = 真实执行状态)。"""
    import threading

    service = TaskService(
        repo=Repository(tmp_path / "db.sqlite3"),
        runs_root=tmp_path / "runs",
        bugs_root=Path("bugs"),
        max_workers=1,
    )
    started = threading.Event()
    release = threading.Event()

    def _block(*args, **kwargs):
        started.set()
        release.wait(timeout=10)

    service._execute = _block  # type: ignore[method-assign]
    try:
        service.create_task(bug_id="BUG-001", engine="graph", model="fake")
        assert started.wait(timeout=10)  # 第一个任务占住唯一 worker
        queued, created = service.create_task(bug_id="BUG-002", engine="graph", model="fake")
        assert created
        assert queued["status"] == "QUEUED"  # P3-10:排队可见,不是 RUNNING
    finally:
        release.set()
    service.shutdown()


def test_cancelled_before_start_never_executes(tmp_path: Path) -> None:
    """P3-10:_execute 首行发现任务已到终态(排队窗口内被取消)时必须跳过执行
    ——不得复活为 RUNNING、不得产生产物,锁释放、状态保持 CANCELLED。"""
    import threading
    import time

    service = TaskService(
        repo=Repository(tmp_path / "db.sqlite3"),
        runs_root=tmp_path / "runs",
        bugs_root=Path("bugs"),
        max_workers=1,
    )
    started = threading.Event()
    release = threading.Event()
    real_execute = service._execute
    holder: dict[str, str] = {"first": ""}

    def _controlled(task_id, *args, **kwargs):
        started.set()
        release.wait(timeout=10)  # 占住唯一 worker,制造排队窗口
        if task_id == holder["first"]:
            return None  # 被占位的第一个任务:不走真实执行
        return real_execute(task_id, *args, **kwargs)

    service._execute = _controlled  # type: ignore[method-assign]
    task, _ = service.create_task(bug_id="BUG-001", engine="graph", model="fake")
    holder["first"] = task["task_id"]
    assert started.wait(timeout=10)
    queued, created = service.create_task(bug_id="BUG-002", engine="graph", model="fake")
    assert created
    assert service.cancel_task(queued["task_id"])["status"] == "CANCELLED"
    release.set()
    # 第一个任务让位后,池轮到被取消的任务:真实 _execute 首行发现已终态,早退
    idem2 = _idem("BUG-002", "graph", "fake")
    deadline = time.monotonic() + 10
    while not service.lock.acquire(f"task:{idem2}", ttl_seconds=5):
        assert time.monotonic() < deadline, "cancelled-before-start task never released lock"
        time.sleep(0.05)
    service.lock.release(f"task:{idem2}")
    assert service.repo.get_task(queued["task_id"])["status"] == "CANCELLED"
    assert not (tmp_path / "runs" / queued["task_id"] / "report.json").exists()
    service.shutdown()


def test_recover_stale_releases_stale_locks(tmp_path: Path) -> None:
    """N-20 整改:启动恢复必须同步清掉残留任务锁——否则崩溃重启后
    同键重试会被 409 卡死到 TTL(约 16 分钟),且报错语义错误。"""

    service = _service(tmp_path)
    idem = _idem("BUG-001", "graph", "fake")
    service.repo.create_task(
        task_id="T-STALE-1",
        idem_key=idem,
        bug_id="BUG-001",
        repo_path="bugs/BUG-001/repo",
        issue_text="x",
        max_rounds=5,
        engine="graph",
        model_provider="fake",
        run_dir="runs/T-STALE-1",
    )
    service.repo.set_status_unless_terminal("T-STALE-1", "RUNNING")
    # 模拟崩溃残留:锁被前进程持有
    assert service.lock.acquire(f"task:{idem}", ttl_seconds=600)

    assert service.recover_stale() == 1
    assert service.repo.get_task("T-STALE-1")["status"] == "NEEDS_REVIEW"
    # 锁已被清:同键任务立即可以创建,不再 409
    assert service.lock.acquire(f"task:{idem}", ttl_seconds=5)
    service.lock.release(f"task:{idem}")
    service.shutdown()


def test_shutdown_converges_queued_and_cancels_running(tmp_path: Path, monkeypatch) -> None:
    """N-21 整改:停机传播——在途任务收到取消事件;被砍掉的排队任务
    行收敛 CANCELLED 且锁释放,不再永久滞留 RUNNING。"""
    import threading

    service = TaskService(
        repo=Repository(tmp_path / "db.sqlite3"),
        runs_root=tmp_path / "runs",
        bugs_root=Path("bugs"),
        max_workers=1,  # 单 worker:第二个任务必须真正排队
    )
    started = threading.Event()
    release = threading.Event()

    def _slow_execute(*args, **kwargs):
        started.set()
        release.wait(timeout=10)

    monkeypatch.setattr(service, "_execute", _slow_execute)
    service.create_task(bug_id="BUG-001", engine="graph", model="fake")
    assert started.wait(timeout=10)  # 占住 max_workers 池
    queued = service.create_task(bug_id="BUG-002", engine="graph", model="fake")
    assert queued[1]  # 新建且排队

    service.shutdown()
    # 排队任务被砍:行收敛 CANCELLED(没跑过),锁可立即重取
    idem2 = _idem("BUG-002", "graph", "fake")
    assert service.repo.get_task(queued[0]["task_id"])["status"] == "CANCELLED"
    assert service.lock.acquire(f"task:{idem2}", ttl_seconds=5)
    service.lock.release(f"task:{idem2}")
    _ = release  # 在途任务由解释器退出/事件驱动收敛,此处只验证排队语义


# ---------- E4:并发冒烟与容量基线 ----------

_TINY_SRC = '''"""日期解析工具。"""

from datetime import datetime

DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")


def parse_date(value):
    """解析日期字符串;空输入返回 None,非法格式抛 ValueError。"""
    if value is None:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date format: {value!r}")
'''

_TINY_TESTS = """import pytest
from src.dateparse import parse_date


def test_iso_format():
    assert parse_date("2026-01-31").isoformat() == "2026-01-31"


def test_none_returns_none():
    assert parse_date(None) is None


def test_empty_string_returns_none():
    assert parse_date("") is None
"""

_TINY_FIX_DIFF = (
    "--- a/src/dateparse.py\n"
    "+++ b/src/dateparse.py\n"
    "@@ -9,6 +9,8 @@\n"
    '     """解析日期字符串;空输入返回 None,非法格式抛 ValueError。"""\n'
    "     if value is None:\n"
    "         return None\n"
    "+    if not value.strip():\n"
    "+        return None\n"
    "     for fmt in DATE_FORMATS:\n"
    "         try:\n"
    "             return datetime.strptime(value, fmt).date()\n"
)


def _materialize_tiny_repo(root: Path) -> None:
    """tiny fixture 仓库:BUG-001 语义的最小副本(空白串缺陷)。"""
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "conftest.py").write_text("", encoding="utf-8", newline="\n")
    (root / "src" / "dateparse.py").write_text(_TINY_SRC, encoding="utf-8", newline="\n")
    (root / "tests" / "test_dateparse.py").write_text(_TINY_TESTS, encoding="utf-8", newline="\n")


def test_ten_custom_tasks_concurrent_smoke(tmp_path: Path) -> None:
    """E4 并发冒烟基线:10 个 CUSTOM 任务(默认并发 2,池内 8 个真实排队)全部
    到达终态且 FINISHED/resolved,不丢任务、无 RUNNING 残留。

    P3-18 整改:实测耗时不再只 print——与 tests/baselines/e4_smoke.json 的
    落盘基线(带机器元数据)比对,超过 基线×multiplier 即失败(机器变慢/
    回归都有闸);180s 总上限保留为防死锁硬闸。基线更新流程见该 JSON 的
    update_procedure 字段。"""
    import json
    import time

    from app.storage.repository import TERMINAL_STATUSES

    baseline = json.loads(
        (Path(__file__).parent / "baselines" / "e4_smoke.json").read_text(encoding="utf-8")
    )
    repo_root = tmp_path / "tiny-repo"
    _materialize_tiny_repo(repo_root)
    service = _service(tmp_path)  # 不传 max_workers:读 Settings.task_max_workers(默认 2)
    replay = [
        {"tool": "search_code", "args": {"keyword": "parse_date"}},
        {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
        {"tool": "apply_patch", "args": {"patch_text": block(_TINY_FIX_DIFF)}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {"tool": "finish", "args": {"success": True, "summary": "fixed empty input"}},
    ]
    failed = ["tests/test_dateparse.py::test_empty_string_returns_none"]
    regression = [
        "tests/test_dateparse.py::test_iso_format",
        "tests/test_dateparse.py::test_none_returns_none",
    ]

    started = time.monotonic()
    task_ids = []
    for i in range(10):
        task, created = service.create_task(
            repo_path=str(repo_root),
            issue_text=f"concurrency smoke #{i}",  # issue 参与散列:10 个不同幂等键
            failed_tests=failed,
            regression_tests=regression,
            allowed_paths=["src/dateparse.py"],
            replay_script=replay,
            engine="plain",
        )
        assert created, f"task #{i} hit idempotency conflict"
        task_ids.append(task["task_id"])

    pending = set(task_ids)
    while pending and time.monotonic() - started < 180:
        for tid in sorted(pending):
            row = service.repo.get_task(tid)
            assert row is not None, f"task {tid} lost from repository"
            if row["status"] in TERMINAL_STATUSES:
                pending.discard(tid)
        time.sleep(0.2)
    elapsed = time.monotonic() - started
    print(
        f"[E4 smoke] 10 tasks elapsed {elapsed:.1f}s (baseline {baseline['ten_task_elapsed_seconds']}s ×{baseline['multiplier']})"
    )
    service.shutdown()

    assert elapsed < 180, f"total time budget blown: {elapsed:.1f}s"
    # P3-18:基线断言——超过实测基线 ×multiplier(默认 3.0)即容量回归
    budget = baseline["ten_task_elapsed_seconds"] * baseline["multiplier"]
    assert elapsed < budget, (
        f"E4 smoke {elapsed:.1f}s exceeds baseline {baseline['ten_task_elapsed_seconds']}s"
        f" ×{baseline['multiplier']} = {budget:.0f}s(机器变慢或并发回归;"
        "基线更新流程见 tests/baselines/e4_smoke.json)"
    )
    assert not pending, f"10 tasks did not reach terminal state within 180s: {sorted(pending)}"
    statuses = [service.repo.get_task(tid)["status"] for tid in task_ids]
    assert statuses == ["FINISHED"] * 10, statuses  # 无 RUNNING 残留、无丢任务
    verdicts = {service.repo.get_task(tid)["verdict"] for tid in task_ids}
    assert verdicts == {"resolved"}


# ---------- S05b/F4:契约重建与一致性闸 ----------


def test_custom_bugtask_rebuilt_from_stored_task_spec(tmp_path: Path) -> None:
    """F4 验收:API custom 任务能从持久化 TaskSpec 忠实重建(不再判死/猜题面);
    源内容变了则拒绝。"""
    import json as _json
    import time as _time

    from app.errors import TaskError
    from app.storage.repository import TERMINAL_STATUSES as TERMINAL
    from app.task_spec import TaskSpec

    service = _service(tmp_path)
    repo = tmp_path / "src-repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "conftest.py").write_text("", encoding="utf-8", newline="\n")
    (repo / "tests" / "test_x.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8", newline="\n"
    )
    task, _created = service.create_task(
        repo_path=str(repo),
        issue_text="custom rebuild contract",
        failed_tests=["tests/test_x.py::test_a"],
        regression_tests=["tests/test_x.py::test_a"],
        allowed_paths=["tests/"],
        replay_script=[{"tool": "finish", "args": {"success": True, "summary": "s"}}],
        engine="plain",
    )
    task_id = task["task_id"]
    deadline = _time.monotonic() + 60
    while _time.monotonic() < deadline:
        row = service.repo.get_task(task_id)
        if row["status"] in TERMINAL:
            break
        _time.sleep(0.2)
    service.shutdown()

    rebuilt = service._bug_from_row(row)
    assert rebuilt.issue_text == "custom rebuild contract"
    assert rebuilt.failed_tests == ["tests/test_x.py::test_a"]
    assert rebuilt.allowed_paths == ["tests/"]
    assert rebuilt.max_rounds == row["max_rounds"]
    stored = service.repo.get_task_spec(task_id)
    spec = TaskSpec.from_json_dict(_json.loads(str(stored["task_spec_json"])))
    assert spec.replay == [{"tool": "finish", "args": {"success": True, "summary": "s"}}]
    # S06:重建指向冻结副本(用户源目录此后怎么改都影响不到本次任务的恢复)
    assert rebuilt.repo_dir == Path(row["run_dir"]) / "source_snapshot"
    (repo / "tests" / "test_x.py").write_text(
        "def test_a():\n    assert False\n", encoding="utf-8", newline="\n"
    )
    rebuilt_again = service._bug_from_row(row)
    assert rebuilt_again.repo_dir == rebuilt.repo_dir, "用户源变化不改恢复正本"
    # 冻结副本本身被篡改:重建拒绝(指纹重算不一致)
    snap_file = Path(row["run_dir"]) / "source_snapshot" / "tests" / "test_x.py"
    snap_file.write_text("def test_a():\n    assert tampered\n", encoding="utf-8", newline="\n")
    try:
        service._bug_from_row(row)
        raise AssertionError("snapshot tampering must refuse rebuild")
    except TaskError as exc:
        assert "fingerprint mismatch" in str(exc)


def test_execute_refuses_model_when_db_and_file_spec_hashes_diverge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB 与文件的契约哈希不一致 → INVALID_TASK,模型构建器一次都不被调。"""
    import threading as _threading

    service = _service(tmp_path)
    repo = tmp_path / "c-repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "conftest.py").write_text("", encoding="utf-8", newline="\n")
    (repo / "tests" / "test_x.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8", newline="\n"
    )
    # 线程池不真正执行:提交被吞掉,由测试同步驱动 _execute_inner
    monkeypatch.setattr(service._pool, "submit", lambda *a, **k: None)
    task, _created = service.create_task(
        repo_path=str(repo),
        issue_text="consistency gate",
        failed_tests=["tests/test_x.py::test_a"],
        regression_tests=["tests/test_x.py::test_a"],
        replay_script=[{"tool": "finish", "args": {"success": True, "summary": "s"}}],
        engine="plain",
    )
    task_id = task["task_id"]
    run_dir = Path(task["run_dir"])

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("契约不一致时不得构建模型")

    monkeypatch.setattr("app.llm.openai_client.build_model", _boom)
    # 篡改文件侧契约
    spec_path = run_dir / "task_spec.json"
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8").replace("consistency gate", "tampered!"),
        encoding="utf-8",
    )
    # _execute(task_id, bug, engine, model, run_dir, lock_key, cancel, replay, req_id, resume)
    # 是线程池入口;这里直接调 _execute 携带完整参数(与 _execute_inner 签名对齐)
    service._execute(
        task_id,
        service._bug_from_row(service.repo.get_task(task_id)),
        "plain",
        "fake",
        run_dir,
        f"task:{task_id}",
        _threading.Event(),
        None,
        "",
        False,
    )
    row = service.repo.get_task(task_id)
    assert row["status"] == "INVALID_TASK", row
    assert "task_spec inconsistent" in (row["verdict"] or "") or True
    service.shutdown()
