"""受理时冻结执行输入(S06):平台自己的只读副本,执行不再读用户可变源目录。

为什么必须有这一层(ADR-0009 §4 的源码身份落到字节):受理之后、执行之前的窗口里,
用户可以改源仓库——旧行为执行的是"执行那一刻"的内容,与受理契约
(task_spec 的 source_snapshot_hash)脱节;恢复重建(ADR-0010 时代码)同样需要一份
稳定的输入正本。冻结后:worker 只从 `run_dir/source_snapshot` 物化 workspace,
用户源目录在任务全程不被写入、不被读取。

规则(与既有边界逐字对齐,不放宽):
- **复制范围 = materialize_repo 的 _TEMPLATE_IGNORE**(`__pycache__`/`.pytest_cache`/
  `*.pyc`/`.git`),规则版本随之记录——需要排除新目录时必须单列变更与测试;
- **哈希实际复制进去的文件字节**(不是 git status、不是路径清单);
- **符号链接一律拒绝**(copy 用 symlinks=True 先按链接保留,再扫描拒绝——
  绝不跟随链接读工作区外文件;拒绝发生在平台的副本上,源目录零接触);
- 源与快照互相包含拒绝(与 materialize_repo 的 P0-3 同一条规则);
- 体量与耗时上限走 Settings(intake_max_files / intake_timeout_seconds);
- tmp 目录 + run_dir 内 rename 原子就位;中途失败清理 tmp,源目录零写入。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from app.errors import TaskError

log = logging.getLogger(__name__)

# 复制规则版本:与 app/gitops/testing.py 的 _TEMPLATE_IGNORE 对齐。
# 修改任何一边的忽略集合都必须同步这里并补测试,否则"冻结的字节"与
# "执行的物化结果"就不是同一份输入。
SNAPSHOT_LAYOUT_VERSION = 1
COPY_IGNORE_PATTERNS = ("__pycache__", ".pytest_cache", "*.pyc", ".git")
SNAPSHOT_DIRNAME = "source_snapshot"


def _containment_guard(source: Path, run_dir: Path) -> None:
    if source == run_dir or source in run_dir.parents or run_dir in source.parents:
        raise TaskError(f"source and run_dir must not contain each other: {source} vs {run_dir}")


def _reject_symlinks(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            if os.path.islink(Path(dirpath) / name):
                rel = Path(dirpath).relative_to(root) / name
                raise TaskError(f"source contains a symlink (refused to follow): {rel.as_posix()}")


def freeze_input(
    source: Path | str,
    run_dir: Path | str,
    *,
    max_files: int,
    timeout_seconds: int,
) -> str:
    """把受理输入复制进 `run_dir/source_snapshot`,返回**副本**的内容指纹。

    - 指纹对复制结果计算(哈希实际进入执行的字节,与 task_spec.fingerprint 同算法);
    - 原子性:先写 `.source_snapshot.tmp-<uuid>`,完整后 rename 就位;
      中途失败清理 tmp,run_dir 里不留下半份快照;
    - 源目录零写入;源里有符号链接/超体量/超时即 TaskError(调用方转 422)。
    """
    src = Path(source).resolve()
    target_root = Path(run_dir).resolve()
    _containment_guard(src, target_root)
    if not src.is_dir():
        raise TaskError(f"source dir missing: {src}")

    if timeout_seconds <= 0:
        raise TaskError("intake_timeout_seconds must be positive")
    started = time.monotonic()
    deadline = started + timeout_seconds
    tmp = target_root / f".{SNAPSHOT_DIRNAME}.tmp-{uuid.uuid4().hex[:8]}"
    final = target_root / SNAPSHOT_DIRNAME
    try:
        shutil.copytree(
            src,
            tmp,
            ignore=shutil.ignore_patterns(*COPY_IGNORE_PATTERNS),
            symlinks=True,  # 链接按链接复制,绝不跟随(下面统一拒绝)
        )
        file_count = sum(len(files) for _, _, files in os.walk(tmp))
        if file_count > max_files:
            raise TaskError(
                f"source too large for intake: {file_count} files > intake_max_files={max_files}"
            )
        if time.monotonic() > deadline:
            raise TaskError(
                f"intake exceeded intake_timeout_seconds={timeout_seconds}; snapshot discarded"
            )
        _reject_symlinks(tmp)
        # 指纹对**副本**计算:哈希实际复制进去的字节(S06 硬要求)
        from app.task_spec import fingerprint_source_dir

        fingerprint = fingerprint_source_dir(tmp)
        if final.exists():
            shutil.rmtree(final)
        os.replace(tmp, final)
    except TaskError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise TaskError(f"intake copy failed: {exc}") from exc
    log.info(
        "intake frozen: %s -> %s (%d ms, layout v%d)",
        src,
        final,
        int((time.monotonic() - started) * 1000),
        SNAPSHOT_LAYOUT_VERSION,
    )
    return fingerprint
