"""运行指标注册表与 Prometheus 暴露 (P13)。

约束：
- 严格保持低基数 (low-cardinality) 标签；
- 严禁将 task_id、secret、token、principal_id 等高基数或敏感信息作为 label；
- 指标覆盖：队列长度与最老等待、活动任务、终态与判定分布、磁盘空闲、预留额度、
  运行恢复失败、证据持久化失败与容器遗留计数。
"""

from __future__ import annotations

import logging
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.storage.repository import Repository

log = logging.getLogger(__name__)


class MetricsRegistry:
    """系统运行指标注册表。"""

    _instance: MetricsRegistry | None = None
    _init_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recovery_failures = 0
        self._evidence_failures = 0
        self._container_residuals = 0
        self._stage_durations: dict[str, list[float]] = {}

    @classmethod
    def get_instance(cls) -> MetricsRegistry:
        with cls._init_lock:
            if cls._instance is None:
                cls._instance = MetricsRegistry()
            return cls._instance

    def record_recovery_failure(self, count: int = 1) -> None:
        with self._lock:
            self._recovery_failures += count

    def record_evidence_failure(self, count: int = 1) -> None:
        with self._lock:
            self._evidence_failures += count

    def record_container_residual(self, count: int = 1) -> None:
        with self._lock:
            self._container_residuals += count

    def record_stage_duration(self, stage: str, duration_seconds: float) -> None:
        clean_stage = str(stage).upper().strip()
        with self._lock:
            samples = self._stage_durations.setdefault(clean_stage, [])
            samples.append(max(0.0, float(duration_seconds)))
            if len(samples) > 200:
                samples.pop(0)

    def collect(
        self,
        repo: Repository | None = None,
        runs_root: Path | str | None = None,
    ) -> dict[str, Any]:
        """采集全部当前指标的结构化字典。"""
        with self._lock:
            recovery_fails = self._recovery_failures
            evidence_fails = self._evidence_failures
            container_residuals = self._container_residuals
            stage_stats = {
                st: {
                    "count": len(vals),
                    "avg_seconds": sum(vals) / len(vals) if vals else 0.0,
                    "max_seconds": max(vals) if vals else 0.0,
                }
                for st, vals in self._stage_durations.items()
            }

        queue_stats = {"pending_count": 0, "claimed_count": 0, "oldest_waiting_seconds": 0.0}
        task_stats: dict[str, Any] = {"active_tasks": 0, "status_counts": {}, "verdict_counts": {}}
        quota_stats = {"total_reserved_usd": 0.0, "total_reserved_tokens": 0}

        if repo is not None:
            try:
                queue_stats = repo.get_queue_stats()
            except Exception as exc:
                log.warning("failed to collect queue stats: %s", exc)
            try:
                task_stats = repo.get_task_stats()
            except Exception as exc:
                log.warning("failed to collect task stats: %s", exc)
            try:
                quota_stats = repo.get_quota_stats()
            except Exception as exc:
                log.warning("failed to collect quota stats: %s", exc)

        free_disk_bytes = 0
        if runs_root is not None:
            try:
                free_disk_bytes = shutil.disk_usage(Path(runs_root)).free
            except Exception as exc:
                log.warning("failed to check disk free bytes: %s", exc)

        return {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "queue": queue_stats,
            "tasks": task_stats,
            "quota": quota_stats,
            "disk": {"free_bytes": free_disk_bytes},
            "failures": {
                "recovery_failures_total": recovery_fails,
                "evidence_persistence_failures_total": evidence_fails,
                "container_residual_total": container_residuals,
            },
            "stages": stage_stats,
        }

    def render_prometheus(
        self,
        repo: Repository | None = None,
        runs_root: Path | str | None = None,
    ) -> str:
        """渲染为标准 Prometheus 文本暴露格式。"""
        data = self.collect(repo, runs_root)
        lines: list[str] = []

        def add_gauge(
            name: str, help_text: str, value: float | int, labels: dict[str, str] | None = None
        ) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            if labels:
                label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
                lines.append(f"{name}{{{label_str}}} {value}")
            else:
                lines.append(f"{name} {value}")

        def add_counter(
            name: str, help_text: str, value: int, labels: dict[str, str] | None = None
        ) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            if labels:
                label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
                lines.append(f"{name}{{{label_str}}} {value}")
            else:
                lines.append(f"{name} {value}")

        # Queue
        q = data["queue"]
        add_gauge(
            "patchpilot_queue_length",
            "Number of jobs in queue by status",
            q.get("pending_count", 0),
            {"status": "pending"},
        )
        add_gauge(
            "patchpilot_queue_length",
            "Number of jobs in queue by status",
            q.get("claimed_count", 0),
            {"status": "claimed"},
        )
        add_gauge(
            "patchpilot_oldest_waiting_seconds",
            "Age in seconds of the oldest pending job in queue",
            q.get("oldest_waiting_seconds", 0.0),
        )

        # Tasks
        t = data["tasks"]
        add_gauge(
            "patchpilot_active_tasks",
            "Current number of non-terminal active tasks",
            t.get("active_tasks", 0),
        )
        for status, count in sorted(t.get("status_counts", {}).items()):
            add_counter(
                "patchpilot_tasks_total",
                "Total tasks recorded by status",
                count,
                {"status": status},
            )
        for verdict, count in sorted(t.get("verdict_counts", {}).items()):
            add_counter(
                "patchpilot_verdicts_total",
                "Total tasks recorded by verdict",
                count,
                {"verdict": verdict},
            )

        # Quota
        qu = data["quota"]
        add_gauge(
            "patchpilot_quota_reserved_usd",
            "Total active reserved quota in USD",
            qu.get("total_reserved_usd", 0.0),
        )
        add_gauge(
            "patchpilot_quota_reserved_tokens",
            "Total active reserved quota in tokens",
            qu.get("total_reserved_tokens", 0),
        )

        # Disk
        d = data["disk"]
        add_gauge(
            "patchpilot_disk_free_bytes",
            "Free storage bytes on artifacts volume",
            d.get("free_bytes", 0),
        )

        # Failures
        f = data["failures"]
        add_counter(
            "patchpilot_recovery_failures_total",
            "Total recovery failures encountered",
            f.get("recovery_failures_total", 0),
        )
        add_counter(
            "patchpilot_evidence_persistence_failures_total",
            "Total evidence persistence/sink failures",
            f.get("evidence_persistence_failures_total", 0),
        )
        add_counter(
            "patchpilot_container_residual_total",
            "Total orphan or residual execution containers detected",
            f.get("container_residual_total", 0),
        )

        # Stages
        for stage, s_stats in sorted(data.get("stages", {}).items()):
            add_gauge(
                "patchpilot_stage_duration_seconds_avg",
                "Average duration in seconds for pipeline stage",
                s_stats["avg_seconds"],
                {"stage": stage},
            )

        return "\n".join(lines) + "\n"
