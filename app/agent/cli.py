"""最小交互入口(G10):issue-only 任务的验收状态查询与批准。

定位:本地单用户、离线优先。真正的产品面是 API(POST /tasks/{id}/acceptance),
本 CLI 是它的一个薄客户端(stdlib urllib,不新增依赖),给"手边没有 curl/脚本"的
使用者一条可复制的批准命令。冻结语义只有一份:CLI 与 HTTP 路由都调用
TaskService.approve_acceptance,不存在第二条写入路径。

用法(默认连本机 127.0.0.1:8001;服务启动见 demo/README.md §3):

    python -m app.agent.cli status  --task-id <id>
    python -m app.agent.cli approve --task-id <id> --proposal-id <pid> \\
        --hash <content_hash> --revision <n> [--token <bearer>]

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
    try:
        method, url, payload, headers = build_request(
            list(argv if argv is not None else sys.argv[1:])
        )
    except SystemExit as exc:  # argparse 的参数错误
        return int(exc.code or 2)
    try:
        status, body = _send(method, url, payload, headers)
    except (urllib.error.URLError, OSError) as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if 200 <= status < 300 else 3


if __name__ == "__main__":
    raise SystemExit(main())
