"""手工演示:gitops 全流程 diff → apply → rollback(T1.5 验收)。

用法:python scripts/demo_gitops.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.gitops.differ import working_tree_diff
from app.gitops.patcher import apply_patch, check_patch
from app.gitops.rollback import reset_workspace, working_tree_is_clean
from app.gitops.snapshot import create_workspace
from app.gitops.testing import materialize_repo

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "demo_repo"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="patchpilot-demo-"))
    print(f"[demo] temp dir: {tmp}")
    try:
        source = tmp / "source"
        head = materialize_repo(FIXTURE, source)
        print(f"[1] 源仓库就绪,HEAD={head[:12]}")

        ws_path = tmp / "ws"
        workspace = create_workspace(source, ws_path)
        print(f"[2] 工作区就绪,基线 commit={workspace[:12]}")

        target = ws_path / "src" / "dateparse.py"
        target.write_text(
            target.read_text(encoding="utf-8") + "\n# demo edit: simulate an agent change\n",
            encoding="utf-8",
        )
        diff = working_tree_diff(ws_path)
        print(f"[3] diff 生成:changed_files={diff.changed_files},diff {len(diff.diff_text)} 字符")

        verify_ws = tmp / "ws2"
        create_workspace(source, verify_ws)
        ok, detail = check_patch(verify_ws, diff.diff_text)
        print(f"[4] 干跑校验:ok={ok}" + (f"({detail})" if not ok else ""))

        result = apply_patch(verify_ws, diff.diff_text)
        print(f"[5] 补丁应用:applied={result.applied} ({result.detail})")

        changed = (verify_ws / "src" / "dateparse.py").read_text(encoding="utf-8")
        print(f"[6] 应用生效:{'demo edit' in changed}")

        reset_workspace(verify_ws, workspace)
        print(f"[7] 回滚完成:工作区干净={working_tree_is_clean(verify_ws)}")

        all_ok = result.applied and "demo edit" in changed and working_tree_is_clean(verify_ws)
        print(f"[done] {'PASS' if all_ok else 'FAIL'}")
        return 0 if all_ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
