"""SWE-bench 数据格式适配器:外部基准 → 内部 BugTask。

边界(如实声明):
- 本模块只做**数据接入**:解析标准 jsonl 字段并映射为 BugTask,复用平台的
  基线/验证/门禁判定;
- 仓库克隆、base_commit 检出、test_patch 应用与题目目录落盘都在
  `scripts/import_swebench.py`(一次性运维脚本,允许联网、人工触发,不进 CI);
- 真实模型运行同样是人工触发。

字段映射:problem_statement→issue_text,FAIL_TO_PASS→failed_tests,
PASS_TO_PASS→regression_tests,导入后的纯工作树目录→repo_dir。
FAIL_TO_PASS / PASS_TO_PASS 在数据集里是 JSON 字符串(如 '["test_x"]'),这里做归一。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.errors import TaskError
from app.evals.bugset import BugTask, validate_test_ids


@dataclass
class SweInstance:
    """SWE-bench 实例的最小字段集(判定只依赖测试清单与描述)。"""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    # gold 补丁与测试补丁(jsonl 原生键,字符串,可缺失):
    # test_patch 缺失 → 基线不成立,整题不可用;patch 缺失 → 只出不回放条目。
    patch: str = ""
    test_patch: str = ""
    # 该实例的官方评测镜像(conda 环境年代精确)。有它才可能在本机复现基线:
    # 现装依赖会装成"今天的最新版",与题目年代不匹配。
    image: str = ""


def _parse_test_list(raw: object, field_name: str, instance_id: str) -> list[str]:
    """数据集里该字段是 JSON 字符串,容错兼容已解析的 list 形态。"""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TaskError(f"{instance_id}: {field_name} is not valid JSON: {exc}") from exc
    elif isinstance(raw, list):
        parsed = raw
    else:
        raise TaskError(f"{instance_id}: {field_name} missing or wrong type")
    if not isinstance(parsed, list) or not all(isinstance(t, str) for t in parsed):
        raise TaskError(f"{instance_id}: {field_name} must be a list of test ids")
    return parsed


def parse_instance(record: dict) -> SweInstance:
    """单条 jsonl 记录 → SweInstance;FAIL_TO_PASS 为空即 TaskError(无病可修)。"""
    instance_id = str(record.get("instance_id", ""))
    if not instance_id:
        raise TaskError("record missing instance_id")
    fail_to_pass = _parse_test_list(record.get("FAIL_TO_PASS"), "FAIL_TO_PASS", instance_id)
    if not fail_to_pass:
        raise TaskError(f"{instance_id}: FAIL_TO_PASS is empty; nothing to verify as fixed")
    return SweInstance(
        instance_id=instance_id,
        repo=str(record.get("repo", "")),
        base_commit=str(record.get("base_commit", "")),
        problem_statement=str(record.get("problem_statement", "")),
        fail_to_pass=fail_to_pass,
        pass_to_pass=_parse_test_list(record.get("PASS_TO_PASS"), "PASS_TO_PASS", instance_id),
        patch=str(record.get("patch", "") or ""),
        test_patch=str(record.get("test_patch", "") or ""),
        image=str(record.get("image", "") or ""),
    )


def load_instances(jsonl_path: Path | str) -> list[SweInstance]:
    """加载 SWE-bench jsonl(每行一个实例);坏行带行号报 TaskError。"""
    path = Path(jsonl_path)
    if not path.exists():
        raise TaskError(f"swebench jsonl not found: {path}")
    instances: list[SweInstance] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TaskError(f"{path.name}:{line_no} is not valid JSON: {exc}") from exc
        try:
            instances.append(parse_instance(record))
        except TaskError as exc:
            raise TaskError(f"{path.name}:{line_no}: {exc}") from exc
    return instances


def to_bug_task(instance: SweInstance, checkout_dir: Path | str) -> BugTask:
    """映射为内部 BugTask:checkout 必须是已就位的本地目录(人工准备)。"""
    repo_dir = Path(checkout_dir)
    if not repo_dir.is_dir():
        raise TaskError(
            f"{instance.instance_id}: local checkout not found: {repo_dir}"
            " (prepare it at the instance's base_commit first)"
        )
    # N-4 整改:jsonl 是外部数据文件,测试 id 与 manifest/API 同罪同防,
    # 否则 FAIL_TO_PASS 里的 argv 片段直达 pytest 命令行
    validate_test_ids(instance.fail_to_pass, instance.instance_id)
    validate_test_ids(instance.pass_to_pass, instance.instance_id)
    return BugTask(
        id=instance.instance_id,
        root=repo_dir,
        repo_dir=repo_dir,
        issue_text=instance.problem_statement,
        failed_tests=instance.fail_to_pass,
        regression_tests=instance.pass_to_pass,
        allowed_paths=None,  # 外部仓库无修改范围先验,禁改测试文件仍由门禁兜底
        max_rounds=5,
        category="swe-bench",
        difficulty="external",
    )
