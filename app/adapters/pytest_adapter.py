"""pytest 适配器:组装命令、执行、解析 JUnit XML 报告。

首版唯一适配器;接口(report 结构、命令组装)为 Maven/Jest 预留,
核心状态机不感知"pytest"字样。
"""

from __future__ import annotations

import logging
import shutil
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from app.adapters.test_identity import TestIdentity, case_matches, parse_node_id
from app.config import get_settings
from app.errors import ExecError
from app.executor.local_runner import TestRunResult, run_tests

log = logging.getLogger(__name__)

# pytest 退出码:0 成功,1 用例失败,2 被中断,3 内部错误,4 用法错误,5 未收集到。
# 判定只依赖 RC_OK 与 RC_NO_TESTS_COLLECTED 两个常量,其余以注释存档。
RC_OK = 0
RC_NO_TESTS_COLLECTED = 5

# 合成 case 的原因尾巴上限,与 junit `<failure>` 正文的截断口径一致(见 parse_junit_xml)
CAUSE_TAIL_CHARS = 4000


@dataclass(frozen=True)
class BugEnv:
    """题目自带的测试执行环境(外部数据集用;由 manifest `env:` 段提供)。

    image+workdir 成对出现 = 容器环境:工作区挂到镜像预装的仓库路径上,`python` 是
    **容器内**绝对路径。只有 python 没有 image = 宿主解释器(自带依赖的虚拟环境)。
    network 只在容器环境有意义,缺省 none(平台隔离边界);放行的取值受枚举限制,
    且只可能来自本地 manifest——模型与 API 请求都写不到这一层。
    """

    python: str | None = None
    image: str | None = None
    workdir: str | None = None
    network: str | None = None

    @property
    def is_container(self) -> bool:
        return bool(self.image and self.workdir)


@dataclass
class FailedCase:
    """单个失败用例及其归一化签名。"""

    test_name: str
    test_id: str
    kind: str  # failure | error
    message_first_line: str
    signature: str
    # 模型可见层用的完整 traceback(junit <failure> 节点正文,截 4000 字符)。
    # 放在末位且带默认值:既有位置参数构造与签名判定口径都不受影响。
    traceback: str = ""


@dataclass
class PytestReport:
    """一次 pytest 执行的结构化报告。"""

    exit_code: int
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    collected: int = 0
    duration_ms: int = 0
    timed_out: bool = False
    no_tests_collected: bool = False
    failed_cases: list[FailedCase] = field(default_factory=list)
    requested_ids: list[str] = field(default_factory=list)
    case_results: list[tuple[str, str, str, str]] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        """判定口径(P0-1 整改;S01/F3 重写匹配语义):请求的每条测试 id 都**真实跑过并通过**,
        且身份无歧义。

        仅凭 exit_code==0 防不了伪:仓库内 conftest 可以把用例 skip 或
        deselect,junit 的 rc 依然是 0。因此:rc==0 且零失败/错误/跳过之外,
        还要求每条 requested_id 都:
        ① 能解析成具体测试身份(解析不了即不通过,预检层本应拒绝);
        ② 在 junit 中恰好对应**一个**去重后的 testcase 三元组——0 个 = 用例缺失/
           被 deselect,≥2 个 = 身份歧义(同后缀不同文件/不同类),都不许通过;
        ③ 该 testcase 的 status 是 passed。
        无期望 id 的调用方退化为"零失败零跳过"。
        """
        if self.exit_code != RC_OK or self.timed_out:
            return False
        if self.failed or self.errors or self.skipped:
            return False
        if not self.requested_ids:
            return True
        identities: list[TestIdentity] = []
        for rid in self.requested_ids:
            identity = parse_node_id(rid)
            if identity is None:
                return False
            identities.append(identity)
        # 去重:同一三元组在 junit 里出现多次只算一个 case;不同三元组各自计数
        distinct_cases: list[tuple[str, str, str]] = []
        for file_attr, classname, case_name, _status in self.case_results:
            case_key = (file_attr, classname, case_name)
            if case_key not in distinct_cases:
                distinct_cases.append(case_key)
        passed_statuses = {
            (file_attr, classname, case_name)
            for file_attr, classname, case_name, status in self.case_results
            if status == "passed"
        }
        for identity in identities:
            matching = [
                case_key for case_key in distinct_cases if case_matches(identity, *case_key)
            ]
            if len(matching) != 1:
                return False  # 缺失(0)或歧义(≥2)
            if matching[0] not in passed_statuses:
                return False
        return True


def build_pytest_cmd(
    python_exe: str,
    test_ids: list[str] | None = None,
    junit_xml: Path | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """组装 pytest 命令(参数列表,供白名单与 runner 使用)。

    junit_family 强制 xunit1:testcase 必须携带 file 属性——all_passed 的
    期望 id 匹配以 file 为主判据(xunit2 不写 file,classname 随 rootdir 漂移)。
    """
    cmd = [python_exe, "-m", "pytest", "-q", "--color=no", "-o", "junit_family=xunit1"]
    if junit_xml is not None:
        cmd.append(f"--junitxml={junit_xml.as_posix()}")
    if extra_args:
        cmd.extend(extra_args)
    if test_ids:
        cmd.extend(test_ids)
    return cmd


def failure_signature(kind: str, message: str) -> str:
    """归一化失败签名:同一失败在多轮之间保持稳定,才能比较"是否还是同一个失败"。

    规则:取消息首行,压缩空白,截断到 160 字符;附失败类别。
    """
    first_line = message.strip().splitlines()[0] if message.strip() else "(no message)"
    normalized = " ".join(first_line.split())[:160]
    return f"{kind}: {normalized}"


def parse_junit_xml(path: Path) -> PytestReport:
    """解析 pytest 生成的 JUnit XML。"""
    report = PytestReport(exit_code=RC_OK)
    if not path.exists():
        report.errors = 1
        report.failed_cases.append(
            FailedCase(
                "(report)", "(report)", "error", "junit xml not generated", "error: no junit xml"
            )
        )
        return report

    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    for suite in suites:
        report.collected += int(suite.get("tests", "0"))
        report.errors += int(suite.get("errors", "0"))
        report.failed += int(suite.get("failures", "0"))
        report.skipped += int(suite.get("skipped", "0"))
        report.passed += report.collected - report.errors - report.failed - report.skipped
        for case in suite.iter("testcase"):
            failure = case.find("failure")
            error = case.find("error")
            skipped = case.find("skipped")
            status = "passed"
            node = None
            if failure is not None:
                status, node = "failure", failure
            elif error is not None:
                status, node = "error", error
            elif skipped is not None:
                status = "skipped"
            report.case_results.append(
                (case.get("file", ""), case.get("classname", ""), case.get("name", "?"), status)
            )
            if node is None:
                continue
            kind = "failure" if failure is not None else "error"
            message = node.get("message", "") or (node.text or "")
            traceback_text = (node.text or "")[:4000]
            test_id = _case_id(case)
            report.failed_cases.append(
                FailedCase(
                    test_name=case.get("name", "?"),
                    test_id=test_id,
                    kind=kind,
                    message_first_line=message.strip().splitlines()[0] if message.strip() else "",
                    signature=failure_signature(kind, message),
                    traceback=traceback_text,
                )
            )
    return report


def attach_missing_report_cause(report: PytestReport, run: TestRunResult, *, subject: str) -> None:
    """junit 没生成时,把退出码与输出尾巴挂到那条合成 case 上(判定口径不读 traceback)。

    `error: no junit xml` 是症状不是原因。docker 后端在 CI 上十四次连红期间,报告里只有
    这一句,真因(`PermissionError: '/reports/junit-….xml'`)在被丢掉的 stderr 里;
    local 后端同形——而且更常碰到,因为**超时是正常业务结果**,被杀时 junit 往往根本没写出来。

    这不新增暴露面:被测仓库的 stdout/stderr 本来就经 `local_runner._truncate` →
    `fold_output` 进模型可见层;junit 的 `<failure>` 正文同样按 4000 字符截断(:198)。
    """
    if not report.failed_cases:
        return
    header = f"{subject}未生成 junit 报告;退出码 {run.exit_code}"
    # 先取 stderr(崩溃 traceback 在那儿),空则退到 stdout(超时现场常只剩 collected 行)
    tail = (run.stderr_tail or "").strip() or (run.stdout_tail or "").strip()
    if not tail:
        report.failed_cases[0].traceback = header
        return
    # 截尾不截头:长输出里"最终异常行"在末尾,而 header 必须留住(否则又回到无信息)
    budget = CAUSE_TAIL_CHARS - len(header)
    report.failed_cases[0].traceback = f"{header}\n{tail[-budget:]}"


def _matches_requested(file_attr: str, classname: str, case_name: str, requested: str) -> bool:
    """junit 的 testcase 是否对应请求的 node id(S01/F3 起委托共享解析器)。

    完整身份语义(文件 + 类链 + 函数 + 参数化)见 app/adapters/test_identity.py;
    请求 id 解析不了(非具体 node id)一律不匹配——旧实现"只对 file+方法名、
    丢类链"的假通过正是本函数的缺陷现场。
    """
    identity = parse_node_id(requested)
    if identity is None:
        return False
    return case_matches(identity, file_attr, classname, case_name)


def _case_id(case: ET.Element) -> str:
    classname = case.get("classname", "")
    name = case.get("name", "")
    return f"{classname}::{name}" if classname else name


def _basetemp_for(junit: Path) -> Path:
    """本次执行私有的临时根:`<报告目录>/<junit 名>.basetemp-<8位随机>`。

    为什么必须"每次执行唯一",而不是按 report_dir 共用一个:pytest 在 `--basetemp`
    已存在时**无条件先整棵删掉再建**(`_pytest/tmpdir.py:154-158`)。两条执行共用同一个
    报告目录就等于共用同一个临时根,后起步那次会把先起步那次**正在写**的 `tmp_path`
    删光 —— 实测 18 次并发里 9 次出现"单独跑能过、并发跑 `FileNotFoundError` 或会话级
    error"(脚本 `scripts/measure_basetemp_contention.py`,判据与数字见 TODO M11.5)。
    随机后缀不是给"同名 junit 跨轮复用"兜底的:超时被杀的会话可能有逃逸的孙子进程还在写
    (`local_runner.py:144-156`),下一轮若复用同一个目录,删它的人会撞上孤儿持有的句柄。
    """
    return junit.parent / f"{junit.stem}.basetemp-{uuid.uuid4().hex[:8]}"


def _discard_basetemp(path: Path) -> None:
    """执行完回收这次的私有临时根(尽力而为,失败不升级为任务失败)。

    显式给了 `--basetemp` 时 pytest 自己的收尾不做清理(`tmpdir.py` 的 finish 只处理
    "没给 basetemp"那一支),所以不回收等于每次执行留一份被诊断仓库的临时产物——
    长任务多轮下来是实打实的磁盘增长。判定用的 junit 与精炼堆栈都在报告目录里,
    与被删的这个目录无关。
    """
    shutil.rmtree(path, ignore_errors=True)


def run_pytest(
    python_exe: str,
    cwd: Path | str,
    test_ids: list[str] | None = None,
    report_path: Path | None = None,
    timeout_seconds: int | None = None,
    extra_args: list[str] | None = None,
    *,
    env: BugEnv | None = None,
) -> tuple[PytestReport, TestRunResult]:
    """执行 pytest 并解析报告;报告缺失/超时都反映在返回值里。

    basetemp 是**这次执行私有**的临时根(落在报告目录下,名字带随机后缀),执行完即回收:
    目标仓库测试里的 tmp_path 不依赖系统临时目录(权限/容量不可控),不污染被验证的工作区,
    也不会和另一条并发执行共用同一个目录(共用的实测后果见 `_basetemp_for`)。
    execution_backend="docker" 时改在临时容器内执行(隔离边界见 docker_runner),
    签名与返回结构不变,上层无感知;镜像需预装 pytest(见 docker/executor.Dockerfile)。
    env 是题目自带环境:容器题覆盖镜像/挂载点/容器内解释器,宿主题覆盖解释器。
    """
    settings = get_settings()
    timeout = timeout_seconds or settings.test_timeout_seconds
    junit = report_path or (Path(cwd) / ".patchpilot_junit.xml")
    if settings.execution_backend == "docker":
        if env is not None and not env.is_container:
            raise ExecError("bug declares a host env.python but execution_backend='docker'")
        return _run_pytest_in_container(
            cwd,
            test_ids,
            junit,
            timeout,
            image=env.image if env else None,
            workdir=env.workdir if env else None,
            python_bin=env.python if env else None,
            network=env.network if env else None,
        )
    if env is not None and env.is_container:
        raise ExecError(
            f"bug requires container env (image={env.image!r}) but execution_backend='local'"
        )
    cmd = build_pytest_cmd(
        env.python if env and env.python else python_exe, test_ids, junit, extra_args
    )
    basetemp = _basetemp_for(junit)
    cmd.append(f"--basetemp={basetemp.as_posix()}")
    try:
        run = run_tests(cmd, cwd, timeout)
    finally:
        _discard_basetemp(basetemp)
    report = parse_junit_xml(junit)
    if not junit.exists():
        attach_missing_report_cause(report, run, subject="被测仓库的 pytest")
    report.requested_ids = list(test_ids or [])
    report.exit_code = run.exit_code
    report.duration_ms = run.duration_ms
    report.timed_out = run.timed_out
    report.no_tests_collected = run.exit_code == RC_NO_TESTS_COLLECTED and not report.timed_out
    log.info(
        "pytest: rc=%s passed=%s failed=%s errors=%s (timed_out=%s)",
        report.exit_code,
        report.passed,
        report.failed,
        report.errors,
        report.timed_out,
    )
    return report, run


def _run_pytest_in_container(
    cwd: Path | str,
    test_ids: list[str] | None,
    junit: Path,
    timeout_seconds: int,
    *,
    image: str | None = None,
    workdir: str | None = None,
    python_bin: str | None = None,
    network: str | None = None,
) -> tuple[PytestReport, TestRunResult]:
    """docker 后端的 pytest 执行:复用 docker_runner 的双挂载与 junit 回传。

    与 local 路径的差异:extra_args 不下发(容器内命令由 docker_runner 组装,
    当前生产调用方未使用该参数);basetemp 用容器内可弃临时目录。
    image/workdir/python_bin 为 None 时退回全局 Settings 与既有默认(/ws + python)。
    """
    from app.executor.docker_runner import docker_available, run_tests_in_container

    if not docker_available():
        raise ExecError("execution_backend='docker' but docker daemon is not available")
    kwargs: dict[str, object] = {}
    if image:
        kwargs["image"] = image
    if workdir:
        kwargs["workdir"] = workdir
    if python_bin:
        kwargs["python_bin"] = python_bin
    if network:
        kwargs["network"] = network
    report, run = run_tests_in_container(
        cwd, test_ids or [], report_dir=junit.parent, timeout_seconds=timeout_seconds, **kwargs
    )
    report.no_tests_collected = run.exit_code == RC_NO_TESTS_COLLECTED and not run.timed_out
    log.info(
        "pytest(容器内): rc=%s passed=%s failed=%s errors=%s (timed_out=%s)",
        report.exit_code,
        report.passed,
        report.failed,
        report.errors,
        report.timed_out,
    )
    return report, run
