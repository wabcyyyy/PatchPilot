"""最小交互入口(G10 批准;G11 澄清/纠偏/取消)。

定位:本地单用户、离线优先。真正的产品面是 API(POST /tasks/{id}/acceptance、
POST /tasks/{id}/messages、POST /tasks/{id}/cancel),本 CLI 是它的一个薄客户端
(stdlib urllib,不新增依赖),给"手边没有 curl/脚本"的使用者一条可复制的命令。
冻结/输入语义只有一份:CLI 与 HTTP 路由都调用 TaskService 的方法,不存在第二条
写入路径。

用法(默认连本机 127.0.0.1:8001;服务启动见 demo/README.md §3):

    python -m app.agent.cli submit --bug-id BUG-001 [--profile repair_v2]
    python -m app.agent.cli submit --repo-path PATH --issue "缺陷描述" \\
        [--failed-tests a,b] [--regression-tests x,y] [--replay-script replay.json]
    python -m app.agent.cli status  --task-id <id>
    python -m app.agent.cli question --task-id <id>
    python -m app.agent.cli answer --task-id <id> --question-id <qid> \\
        --revision <n> --text "回答内容" [--idem-key <k>]
    python -m app.agent.cli steer  --task-id <id> --text "补充线索" [--idem-key <k>]
    python -m app.agent.cli cancel --task-id <id>
    python -m app.agent.cli events --task-id <id> [--last-event-id <n>] [--json]
    python -m app.agent.cli approve --task-id <id> --proposal-id <pid> \\
        --hash <content_hash> --revision <n> [--token <bearer>]

submit(G16):创建任务的新产品入口,默认 profile=repair_v2(显式随请求发送,
不靠服务端环境变量猜新旧客户端);老口径用 --profile legacy_v1 显式退回。
status 在任务处于等待时额外拉取 /question,把问题与选项一起打印,并给出回答/取消
的命令提示;不改变原有 JSON 输出形状。

退出码:0 成功;2 参数错误;3 请求失败或非 2xx 响应。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_API = "http://127.0.0.1:8001"


def _split_tests(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _validate_submit(args: argparse.Namespace) -> None:
    """submit 入参的矛盾在客户端就地报错(exit 2),不打到服务端才 422。"""
    bad = ""
    if (args.bug_id is None) == (args.repo_path is None):
        bad = "exactly one of --bug-id or --repo-path is required"
    elif args.repo_path is not None and not args.issue:
        bad = "--repo-path requires --issue"
    elif bool(args.failed_tests) != bool(args.regression_tests):
        bad = "--failed-tests and --regression-tests must be provided together"
    if bad:
        print(f"argument error: {bad}", file=sys.stderr)
        raise SystemExit(2)


def _submit_payload(args: argparse.Namespace) -> dict[str, object]:
    """submit → POST /api/tasks 请求体;profile 显式随请求发送(G16)。"""
    payload: dict[str, object] = {
        "engine": args.engine,
        "model": args.model,
        "agent_profile": args.profile,
        "arm": "agent",
    }
    if args.bug_id:
        payload["bug_id"] = args.bug_id
    else:
        payload["repo_path"] = args.repo_path
        payload["issue_text"] = args.issue
        failed = _split_tests(args.failed_tests)
        if failed:
            payload["failed_tests"] = failed
            payload["regression_tests"] = _split_tests(args.regression_tests)
        if args.replay_script:
            try:
                payload["replay_script"] = json.loads(
                    Path(args.replay_script).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                print(f"argument error: cannot read --replay-script: {exc}", file=sys.stderr)
                raise SystemExit(2) from None
    if args.max_rounds is not None:
        payload["max_rounds"] = args.max_rounds
    return payload


def build_request(
    argv: list[str],
) -> tuple[str, str, dict[str, object] | None, dict[str, str]]:
    """解析 CLI 参数 → (method, url_path, payload, headers);纯函数,可离线测试。"""
    parser = argparse.ArgumentParser(prog="app.agent.cli", description="PatchPilot 最小交互入口")
    # --api/--token 挂在子命令上(用户的自然写法是 `approve --task-id ... --api ...`;
    # 挂主解析器时 argparse 只允许放在子命令之前)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api", default=DEFAULT_API, help="服务基址(默认本机 8001)")
    common.add_argument("--token", default="", help="Bearer token(服务端启用鉴权时必填)")
    common.add_argument("--api-key", default="", help="API Key(与 --token 互为别名)")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser(
        "submit", parents=[common], help="创建任务(新产品入口;默认 profile=repair_v2)"
    )
    submit.add_argument("--bug-id", default=None, help="bugs/ 下的题目编号,如 BUG-001")
    submit.add_argument(
        "--repo-path", default=None, help="自定义 Git 仓库路径;与 --bug-id 恰好一个"
    )
    submit.add_argument("--issue", default=None, help="缺陷描述(repo_path 时必填)")
    submit.add_argument(
        "--failed-tests", default="", help="逗号分隔的基线失败测试;与 --regression-tests 成对"
    )
    submit.add_argument(
        "--regression-tests", default="", help="逗号分隔的基线通过测试;双缺 = issue-only 受理"
    )
    submit.add_argument(
        "--replay-script", default="", help="fake 自定义任务的回放脚本 JSON 文件路径"
    )
    submit.add_argument("--engine", choices=["graph", "plain"], default="graph")
    submit.add_argument("--model", choices=["fake", "openai"], default="fake")
    submit.add_argument(
        "--profile",
        choices=["repair_v2", "legacy_v1"],
        default="repair_v2",
        help="运行 profile;MVP 新任务默认 repair_v2(显式发送,不靠服务端猜)",
    )
    submit.add_argument("--max-rounds", type=int, default=None)

    status = sub.add_parser(
        "status", parents=[common], help="查询任务状态(diagnose 任务可见 WAITING_INPUT)"
    )
    status.add_argument("--task-id", required=True)

    question = sub.add_parser(
        "question", parents=[common], help="查看当前未回答的澄清问题与输入版本"
    )
    question.add_argument("--task-id", required=True)

    answer = sub.add_parser(
        "answer", parents=[common], help="回答 Agent 的澄清问题(冻结输入版本,防过期)"
    )
    answer.add_argument("--task-id", required=True)
    answer.add_argument("--question-id", required=True)
    answer.add_argument("--revision", type=int, required=True, help="question 输出的输入版本")
    answer.add_argument("--text", required=True)
    answer.add_argument("--idem-key", default="", help="重复提交的幂等键(可选)")

    steer = sub.add_parser(
        "steer", parents=[common], help="追加途中纠偏(只作为线索,不改测试/权限/预算)"
    )
    steer.add_argument("--task-id", required=True)
    steer.add_argument("--text", required=True)
    steer.add_argument("--idem-key", default="")

    cancel = sub.add_parser("cancel", parents=[common], help="取消任务(终态不可复活)")
    cancel.add_argument("--task-id", required=True)

    events = sub.add_parser("events", parents=[common], help="跟踪任务事件流(SSE;终态帧后自动退出)")
    events.add_argument("--task-id", required=True)
    events.add_argument(
        "--last-event-id", type=int, default=0, help="只收该事件行之后的事件(断连续传)"
    )
    events.add_argument("--json", action="store_true", help="逐帧打印原始 JSON data")

    approve = sub.add_parser(
        "approve", parents=[common], help="批准验收清单提议(冻结 manifest 并放行修复)"
    )
    approve.add_argument("--task-id", required=True)
    approve.add_argument("--proposal-id", required=True)
    approve.add_argument("--hash", required=True, help="提议 content_hash(批准目标)")
    approve.add_argument("--revision", type=int, required=True, help="提议 revision")

    args = parser.parse_args(argv)
    headers: dict[str, str] = {"Accept": "application/json"}
    auth_token = args.token or getattr(args, "api_key", "")
    if not auth_token:
        import os

        auth_token = os.environ.get("PATCHPILOT_API_KEY", "") or os.environ.get(
            "PATCHPILOT_API_TOKEN", ""
        )
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    base = str(args.api).rstrip("/")
    if args.command == "submit":
        _validate_submit(args)
        headers["Content-Type"] = "application/json"
        return "POST", f"{base}/api/tasks", _submit_payload(args), headers
    if args.command == "status":
        return "GET", f"{base}/api/tasks/{args.task_id}", None, headers
    if args.command == "question":
        return "GET", f"{base}/api/tasks/{args.task_id}/question", None, headers
    if args.command == "cancel":
        return "POST", f"{base}/api/tasks/{args.task_id}/cancel", None, headers
    if args.command == "events":
        return (
            "GET",
            f"{base}/api/tasks/{args.task_id}/events?last_event_id={max(int(args.last_event_id), 0)}",
            None,
            headers,
        )
    if args.command == "answer":
        headers["Content-Type"] = "application/json"
        return (
            "POST",
            f"{base}/api/tasks/{args.task_id}/messages",
            {
                "kind": "answer",
                "text": args.text,
                "question_id": args.question_id,
                "expected_revision": args.revision,
                "idem_key": args.idem_key,
            },
            headers,
        )
    if args.command == "steer":
        headers["Content-Type"] = "application/json"
        return (
            "POST",
            f"{base}/api/tasks/{args.task_id}/messages",
            {"kind": "steer", "text": args.text, "idem_key": args.idem_key},
            headers,
        )
    payload: dict[str, object] = {
        "proposal_id": args.proposal_id,
        "content_hash": args.hash,
        "expected_revision": args.revision,
    }
    headers["Content-Type"] = "application/json"
    return "POST", f"{base}/api/tasks/{args.task_id}/acceptance", payload, headers


def _send(
    method: str, url: str, payload: dict[str, object] | None, headers: dict[str, str]
) -> tuple[int, dict[str, object]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"code": "http_error", "message": body[:500]}


def _handle_admin(argv: list[str]) -> int:
    """本地受保护管理命令: 创建主体与凭据生命周期操作，禁止通过网络匿名操作。"""
    from datetime import UTC, datetime, timedelta

    from app.config import get_settings
    from app.storage.repository import Repository

    parser = argparse.ArgumentParser(
        prog="app.agent.cli admin",
        description="PatchPilot 本地管理命令 (主体与凭据管理，禁止开匿名 HTTP 口)",
    )
    parser.add_argument("--db", default="", help="SQLite 数据库路径 (默认读 Settings)")
    sub = parser.add_subparsers(dest="action", required=True)

    p_create = sub.add_parser("create-principal", help="创建主体")
    p_create.add_argument("--id", required=True, help="主体 ID")
    p_create.add_argument("--name", required=True, help="主体名称")
    p_create.add_argument(
        "--role",
        choices=["admin", "member", "service_account", "reviewer"],
        default="member",
        help="角色",
    )

    k_issue = sub.add_parser("issue-key", help="签发 API Key")
    k_issue.add_argument("--principal-id", required=True, help="主体 ID")
    k_issue.add_argument("--expires-days", type=int, default=None, help="有效期天数")

    k_revoke = sub.add_parser("revoke-key", help="吊销 API Key")
    k_revoke.add_argument("--key-id", required=True, help="凭据 ID (如 cr_...)")

    k_rotate = sub.add_parser("rotate-key", help="轮换 API Key")
    k_rotate.add_argument("--key-id", required=True, help="原凭据 ID")
    k_rotate.add_argument("--grace-seconds", type=int, default=0, help="旧 Key 宽限存活秒数")

    k_list = sub.add_parser("list-keys", help="列举凭据")
    k_list.add_argument("--principal-id", default=None, help="按主体筛选")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 2)

    db_path = args.db or get_settings().db_path
    repo = Repository(db_path)

    if args.action == "create-principal":
        principal = repo.create_principal(principal_id=args.id, name=args.name, role=args.role)
        print(json.dumps(principal, ensure_ascii=False, indent=2))
        return 0

    if args.action == "issue-key":
        expires_at = None
        if args.expires_days:
            expires_at = (datetime.now(UTC) + timedelta(days=args.expires_days)).isoformat(
                timespec="milliseconds"
            )
        cred, raw_key = repo.issue_credential(principal_id=args.principal_id, expires_at=expires_at)
        out = {
            "credential": cred,
            "api_key": raw_key,
            "notice": "请妥善保管该 API Key，仅显示一次，无法找回。",
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.action == "revoke-key":
        success = repo.revoke_credential(args.key_id)
        if not success:
            print(f"error: key not found or already revoked: {args.key_id}", file=sys.stderr)
            return 3
        print(json.dumps({"revoked": True, "credential_id": args.key_id}))
        return 0

    if args.action == "rotate-key":
        try:
            new_cred, new_raw_key = repo.rotate_credential(
                args.key_id, grace_seconds=args.grace_seconds
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
        out = {
            "new_credential": new_cred,
            "new_api_key": new_raw_key,
            "rotated_from": args.key_id,
            "grace_seconds": args.grace_seconds,
            "notice": "请妥善保管新 API Key，仅显示一次。",
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.action == "list-keys":
        keys = repo.list_credentials(principal_id=args.principal_id)
        print(json.dumps(keys, ensure_ascii=False, indent=2))
        return 0

    return 2


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    if raw and raw[0] == "admin":
        return _handle_admin(raw[1:])
    try:
        method, url, payload, headers = build_request(raw)
    except SystemExit as exc:  # argparse 的参数错误
        return int(exc.code or 2)
    if raw and raw[0] == "events":
        return _stream_events(url, headers, as_json="--json" in raw)
    try:
        status, body = _send(method, url, payload, headers)
    except (urllib.error.URLError, OSError) as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 3
    # G11:status 在等待时补一屏"问题/选项/怎么回答/怎么取消"(原地打印,不改 JSON 形状)
    if raw and raw[0] == "status" and 200 <= status < 300 and isinstance(body, dict):
        _print_waiting_hint(url, headers, body)
    if raw and raw[0] == "submit" and 200 <= status < 300 and isinstance(body, dict):
        _print_submit_hint(body)
    print(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if 200 <= status < 300 else 3


def _stream_events(url: str, headers: dict[str, str], *, as_json: bool = False) -> int:
    """跟踪 SSE 事件流:逐帧打印,收到终态帧自动退出(G12 的 CLI 侧薄客户端)。

    与 API 看到的是同一组事实(同一张轨迹表);`--last-event-id` 供断连重连续传。
    读流本身失败(网络/旧服务)按退出码 3 处理,不假装"看完了"。
    """
    request = urllib.request.Request(
        url, headers={**headers, "Accept": "text/event-stream"}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=3600) as resp:
            event_type = ""
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("event: "):
                    event_type = line[len("event: ") :]
                    continue
                if line.startswith("data: "):
                    data = line[len("data: ") :]
                    if as_json:
                        print(data)
                    else:
                        _print_frame(event_type, data)
                    if event_type == "task_status":
                        try:
                            payload = json.loads(data)
                        except json.JSONDecodeError:
                            payload = {}
                        if payload.get("terminal"):
                            return 0
                    continue
    except KeyboardInterrupt:
        return 0
    except (urllib.error.URLError, OSError) as exc:
        print(f"event stream failed: {exc}", file=sys.stderr)
        return 3
    return 0


def _print_frame(event_type: str, data: str) -> None:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        print(f"{event_type:14s} {data[:200]}")
        return
    stage = payload.get("stage") or "-"
    tool = payload.get("tool") or "-"
    error = f"  error={payload['error']}" if payload.get("error") else ""
    status = payload.get("status") or ""
    verdict = payload.get("verdict") or ""
    tail = f"  {status}/{verdict}" if event_type == "task_status" else ""
    print(f"{event_type:14s} {stage:12s} {tool}{tail}{error}")


def _print_submit_hint(task: dict[str, object]) -> None:
    """submit 成功后给两条后续命令;失败(3xx/4xx/5xx)时什么都不加。"""
    task_id = task.get("task_id")
    print(f"== 已受理 {task_id} ==")
    print(f"跟踪: python -m app.agent.cli events --task-id {task_id}")
    print(f"状态: python -m app.agent.cli status --task-id {task_id}")


def _print_waiting_hint(url: str, headers: dict[str, str], task: dict[str, object]) -> None:
    """任务在等待输入时打印问题、选项与回答/取消命令;非等待态什么都不做。

    问题拉取失败(旧服务/瞬态错误)静默跳过:提示是附加物,不得让 status 本身失败。
    """
    if task.get("status") != "WAITING_INPUT":
        return
    try:
        code, question = _send("GET", url + "/question", None, headers)
    except (urllib.error.URLError, OSError):
        return
    if not (200 <= code < 300) or not isinstance(question, dict):
        return
    item = question.get("question")
    if isinstance(item, dict):
        print(f"== 等待用户回答 ==\n问题: {item.get('question')}")
        choices = item.get("choices") or []
        if choices:
            print("选项: " + " / ".join(str(c) for c in choices))
        print(
            "回答: python -m app.agent.cli answer"
            f" --task-id {task.get('task_id')} --question-id {item.get('question_id')}"
            f' --revision {item.get("revision")} --text "..."'
        )
    print(f"取消: python -m app.agent.cli cancel --task-id {task.get('task_id')}")


if __name__ == "__main__":
    raise SystemExit(main())
