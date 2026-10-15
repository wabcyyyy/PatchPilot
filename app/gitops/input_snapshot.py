"""受理时冻结执行输入 (S06/P05): 平台自己的只读副本, 执行不再读用户可变源目录。

设计与边界 (ADR-0009 §4, P05 规格):
- 复制策略版本化:
  - V1 (development): 排除 __pycache__, .pytest_cache, *.pyc, .git (与既有契约一致);
  - V2 (production): 额外排除 .venv/node_modules 等本机依赖与敏感秘密 (*.key, *.pem, .env* 等),
    显式保留 lock 依赖锁文件 (poetry.lock, Pipfile.lock, requirements*.txt 等);
- 有界收敛与资源保护:
  - 遍历与复制期间流式检测单文件大小、文件数、总字节数、耗时及可用磁盘空间;
  - 遇到符号链接或特殊文件立即拒绝, 绝不跟随跨工作区链接;
  - 遇到巨文件或超体量在边界处立即中断抛出 TaskError, 绝不先复制完再拒绝;
- 源码指纹同源流式计算:
  - 在复制流中边写边算 SHA-256, 校验写入前后尺寸一致性 (检测源并发漂移);
  - 产出指纹与 task_spec.fingerprint_source_dir 算法和忽略规则严格同构;
- 事务性与不可变性:
  - 写入临时目录 .source_snapshot.tmp-<uuid>, 校验成功后原子 rename 就位;
  - 任何失败立即清理临时目录, 源目录全程只读, 零接触零修改。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from app.errors import TaskError

log = logging.getLogger(__name__)

SNAPSHOT_LAYOUT_V1 = 1
SNAPSHOT_LAYOUT_V2 = 2
SNAPSHOT_LAYOUT_VERSION = SNAPSHOT_LAYOUT_V1
SNAPSHOT_DIRNAME = "source_snapshot"

COPY_IGNORE_PATTERNS = ("__pycache__", ".pytest_cache", "*.pyc", ".git")

# 生产模式忽略列表
PROD_IGNORE_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".venv",
        "venv",
        "env",
        ".env",
        "node_modules",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".ruff_cache",
    }
)
PROD_SECRET_EXTS = frozenset({".pem", ".key", ".pfx", ".p12"})
PROD_SECRET_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")


def is_ignored_path(rel_path: str, is_dir: bool, mode: str = "development") -> bool:
    """判定相对路径在指定模式下是否属于排除集合。"""
    parts = Path(rel_path).parts
    if mode == "production":
        if is_dir:
            return any(p in PROD_IGNORE_DIRS for p in parts)
        # 文件判定
        if any(p in PROD_IGNORE_DIRS for p in parts[:-1]):
            return True
        name = parts[-1]
        if name.endswith(".pyc"):
            return True
        # 排除 .env* 秘密文件，但保留 .env.example
        if name.startswith(".env") and name != ".env.example":
            return True
        if Path(name).suffix.lower() in PROD_SECRET_EXTS:
            return True
        if any(name.startswith(prefix) for prefix in PROD_SECRET_PREFIXES):
            return True
        # 排除无扩展名的 credentials 秘密，但保留含 .lock/.json/.py 结尾的普通代码与配置
        lower_name = name.lower()
        return bool(
            "credentials" in lower_name
            and not (
                lower_name.endswith(".lock")
                or lower_name.endswith(".json")
                or lower_name.endswith(".py")
                or lower_name.endswith(".example")
            )
        )

    # development 模式 (V1)
    if is_dir:
        return any(p in {".git", "__pycache__", ".pytest_cache"} for p in parts)
    if any(p in {".git", "__pycache__", ".pytest_cache"} for p in parts[:-1]):
        return True
    return parts[-1].endswith(".pyc")


def _containment_guard(source: Path, run_dir: Path) -> None:
    if source == run_dir or source in run_dir.parents or run_dir in source.parents:
        raise TaskError(f"source and run_dir must not contain each other: {source} vs {run_dir}")


def freeze_input(
    source: Path | str,
    run_dir: Path | str,
    *,
    max_files: int,
    timeout_seconds: int,
    max_file_bytes: int = 10_000_000,
    max_total_bytes: int = 100_000_000,
    min_free_disk_bytes: int = 200_000_000,
    mode: str = "development",
) -> str:
    """把受理输入复制进 `run_dir/source_snapshot`, 返回**副本**的内容指纹。

    有界防护:
    - 遍历中实时校验单文件上限、总文件数、总字节数、耗时及可用磁盘空间;
    - 遇到符号链接或特殊文件即刻拒绝;
    - 原子就位: 异常立即清理 tmp 临时目录, 源目录零写入。
    """
    src = Path(source).resolve()
    target_root = Path(run_dir).resolve()
    _containment_guard(src, target_root)
    if not src.is_dir():
        raise TaskError(f"source dir missing: {src}")

    if timeout_seconds <= 0:
        raise TaskError("intake_timeout_seconds must be positive")

    # 磁盘空间前置检查 (检查目标路径或其最近已存在的祖先目录)
    check_dir = target_root
    while not check_dir.exists() and check_dir.parent != check_dir:
        check_dir = check_dir.parent
    try:
        usage = shutil.disk_usage(check_dir)
        if usage.free < min_free_disk_bytes:
            raise TaskError(
                f"insufficient disk space on {target_root}: {usage.free} bytes free < min={min_free_disk_bytes}"
            )
    except OSError as exc:
        raise TaskError(f"cannot inspect disk space on {target_root}: {exc}") from exc

    started = time.monotonic()
    deadline = started + timeout_seconds
    tmp = target_root / f".{SNAPSHOT_DIRNAME}.tmp-{uuid.uuid4().hex[:8]}"
    final = target_root / SNAPSHOT_DIRNAME

    file_count = 0
    total_bytes = 0
    entries: list[list[str]] = []

    try:
        tmp.mkdir(parents=True, exist_ok=True)
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            if time.monotonic() > deadline:
                raise TaskError(
                    f"intake exceeded intake_timeout_seconds={timeout_seconds}; snapshot discarded"
                )
            rel_dir = Path(dirpath).relative_to(src).as_posix()

            # 目录剪枝过滤
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not is_ignored_path(
                    f"{rel_dir}/{d}" if rel_dir != "." else d, is_dir=True, mode=mode
                )
            )

            for name in sorted(filenames):
                if time.monotonic() > deadline:
                    raise TaskError(
                        f"intake exceeded intake_timeout_seconds={timeout_seconds}; snapshot discarded"
                    )
                rel = f"{rel_dir}/{name}" if rel_dir != "." else name
                if is_ignored_path(rel, is_dir=False, mode=mode):
                    continue

                full_src = Path(dirpath) / name
                if full_src.is_symlink():
                    raise TaskError(f"source contains a symlink (refused to follow): {rel}")
                if not full_src.is_file():
                    raise TaskError(f"source contains special non-file (refused): {rel}")

                file_count += 1
                if file_count > max_files:
                    raise TaskError(
                        f"source too large for intake: {file_count} files > intake_max_files={max_files}"
                    )

                st = full_src.stat()
                if st.st_size > max_file_bytes:
                    raise TaskError(
                        f"file too large for intake: {rel} ({st.st_size} bytes > max={max_file_bytes})"
                    )
                total_bytes += st.st_size
                if total_bytes > max_total_bytes:
                    raise TaskError(
                        f"source total bytes exceeded: {total_bytes} bytes > max={max_total_bytes}"
                    )

                # 流式分块复制，计算 SHA-256 并校验尺寸一致性
                dest_file = tmp / rel
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                hasher = hashlib.sha256()
                copied_bytes = 0

                with open(full_src, "rb") as f_in, open(dest_file, "wb") as f_out:
                    while True:
                        if time.monotonic() > deadline:
                            raise TaskError(
                                f"intake exceeded intake_timeout_seconds={timeout_seconds}; snapshot discarded"
                            )
                        chunk = f_in.read(65536)
                        if not chunk:
                            break
                        copied_bytes += len(chunk)
                        if copied_bytes > max_file_bytes:
                            raise TaskError(
                                f"file too large for intake: {rel} (> max={max_file_bytes})"
                            )
                        hasher.update(chunk)
                        f_out.write(chunk)

                if copied_bytes != st.st_size:
                    raise TaskError(
                        f"source file mutated during intake: {rel} (stat={st.st_size}B != copied={copied_bytes}B)"
                    )

                entries.append([rel, "file", hasher.hexdigest()])

        from app.task_spec import stable_hash

        layout_v = SNAPSHOT_LAYOUT_V2 if mode == "production" else SNAPSHOT_LAYOUT_V1
        fingerprint = stable_hash({"root_version": layout_v, "entries": entries})

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
        "intake frozen: %s -> %s (%d files, %d bytes, %d ms, layout v%d)",
        src,
        final,
        file_count,
        total_bytes,
        int((time.monotonic() - started) * 1000),
        layout_v,
    )
    return fingerprint
