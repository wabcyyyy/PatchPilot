"""最小交互入口(G10 批准;G11 澄清/纠偏/取消)。

定位:本地单用户、离线优先。真正的产品面是 API(POST /tasks/{id}/acceptance、
POST /tasks/{id}/messages、POST /tasks/{id}/cancel),本 CLI 是它的一个薄客户端
(stdlib urllib,不新增依赖),给"手边没有 curl/脚本"的使用者一条可复制的命令。
冻结/输入语义只有一份:CLI 与 HTTP 路由都调用 TaskService 的方法,不存在第二条
写入路径。

用法(默认连本机 127.0.0.1:8001;服务启动见 demo/README.md §3):

    python -m app.agent.cli status  --task-id <id>
    python -m app.agent.cli question --task-id <id>
    python -m app.agent.cli answer --task-id <id> --question-id <qid> \\
        --revision <n> --text "回答内容" [--idem-key <k>]
    python -m app.agent.cli steer  --task-id <id> --text "补充线索" [--idem-key <k>]
    python -m app.agent.cli cancel --task-id <id>
    python -m app.agent.cli approve --task-id <id> --proposal-id <pid> \\
        --hash <content_hash> --revision <n> [--token <bearer>]

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

DEFAULT_API = "http://127.0.0.1:8001"


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
    sub = parser.add_subparsers(dest="command", required=True)

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

    approve = sub.add_parser(
        "approve", parents=[common], help="批准验收清单提议(冻结 manifest 并放行修复)"
    )
    approve.add_argument("--task-id", required=True)
    approve.add_argument("--proposal-id", required=True)
    approve.add_argument("--hash", required=True, help="提议 content_hash(批准目标)")
    approve.add_argument("--revision", type=int, required=True, help="提议 revision")

    args = parser.parse_args(argv)
    headers: dict[str, str] = {"Accept": "application/json"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    base = str(args.api).rstrip("/")
    if args.command == "status":
        return "GET", f"{base}/api/tasks/{args.task_id}", None, headers
    if args.command == "question":
        return "GET", f"{base}/api/tasks/{args.task_id}/question", None, headers
    if args.command == "cancel":
        return "POST", f"{base}/api/tasks/{args.task_id}/cancel", None, headers
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


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    try:
        method, url, payload, headers = build_request(raw)
    except SystemExit as exc:  # argparse 的参数错误
        return int(exc.code or 2)
    try:
        status, body = _send(method, url, payload, headers)
    except (urllib.error.URLError, OSError) as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 3
    # G11:status 在等待时补一屏"问题/选项/怎么回答/怎么取消"(原地打印,不改 JSON 形状)
    if raw and raw[0] == "status" and 200 <= status < 300 and isinstance(body, dict):
        _print_waiting_hint(url, headers, body)
    print(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if 200 <= status < 300 else 3


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
