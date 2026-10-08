"""度量脚本:真实 SWE 语料里"必改文件"是否落在检索遍历域内(零成本、不跑模型)。

为什么需要它:D.12 发现旧版 `MAX_LIST_FILES=500` 同时裁**遍历域**,于是排序第 500 之后的
文件对模型彻底不可见。M3.6 把遍历域与输出体量分设上限(`Settings.max_search_files`),
这条脚本就是**用真实仓库复查分界是否真的生效** —— 单测只能证明形状,证明不了真实仓库的位次。

跑法(需要 `.pytest-tmp/base-SWE-*/repo` 这份基线缓存在场,它是定向用例跑出来的临时产物,
不在场时会如实报告"可测题数为 0"而不是假装通过):

    PYTHONPATH=. python scripts/measure_retrieval_domain.py

判读口径:每一题每个必改文件给出它在遍历域里的位次;位次 ≥ 旧上限 500 的记为
"旧上限下不可见"(即被这条缺陷挡在门外的文件),`truncated=True` 表示连新上限都不够。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings  # noqa: E402
from app.context.ast_outline import parse_source, symbol_entries  # noqa: E402
from app.tools.files import MAX_LIST_FILES, search_scope  # noqa: E402

DATASETS = ("data/swe_hard10.jsonl", "data/swe10.jsonl")


def cached_repos() -> dict[str, Path]:
    """基线缓存目录名 = `base-SWE-<instance_id 里 / 换 ->` + mktemp 的序号后缀。"""
    out: dict[str, Path] = {}
    for path in REPO_ROOT.joinpath(".pytest-tmp").glob("base-SWE-*"):
        repo = path / "repo"
        if repo.is_dir():
            out[path.name] = repo
    return out


def repo_for(instance_id: str, cache: dict[str, Path]) -> Path | None:
    stem = "base-SWE-" + instance_id.replace("/", "-")
    for name, repo in cache.items():
        if name.startswith(stem) and name[len(stem) :].isdigit():
            return repo
    return None


def gold_files(patch: str) -> list[str]:
    return [line[6:].strip() for line in patch.splitlines() if line.startswith("+++ b/")]


def main() -> int:
    cache = cached_repos()
    if not cache:
        print("没有 base-SWE-* 基线缓存,无法度量(先跑一次 corpus 定向用例)")
        return 0
    limit = get_settings().max_search_files
    print(
        f"缓存仓库 {len(cache)} 个 | 遍历域上限 max_search_files={limit} | 旧列表上限={MAX_LIST_FILES}"
    )

    measured = files = invisible = absent = truncated = parsed = failed = symbols = 0
    worst_seen = 0
    for dataset in DATASETS:
        path = REPO_ROOT / dataset
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            instance_id = str(row["instance_id"])
            repo = repo_for(instance_id, cache)
            if repo is None:
                continue
            rels, hit_limit = search_scope(repo)
            order = {rel: index for index, rel in enumerate(rels)}
            measured += 1
            truncated += bool(hit_limit)
            marks = []
            for target in gold_files(str(row["patch"])):
                files += 1
                index = order.get(target)
                if index is None:
                    absent += 1
                    marks.append(f"{target}=缺失")
                    continue
                worst_seen = max(worst_seen, index)
                old = index >= MAX_LIST_FILES
                invisible += old
                marks.append(f"{target}=第{index}{'(旧上限外)' if old else ''}")
                if target.endswith(".py"):
                    tree, error = parse_source(
                        (repo / target).read_text(encoding="utf-8", errors="replace"), target
                    )
                    if tree is None:
                        failed += 1
                        marks[-1] += f"(AST 失败:{error})"
                    else:
                        parsed += 1
                        symbols += len(symbol_entries(tree))
            print(f"  {instance_id:<32} 遍历 {len(rels):>5}(裁过={hit_limit}) {'; '.join(marks)}")
    print(
        f"合计:可测 {measured} 题 / 必改文件 {files} 个;"
        f"旧上限下不可见 {invisible} 个,遍历域内仍缺失 {absent} 个,触到上限的题 {truncated} 个,"
        f"全域最大位次 {worst_seen}"
    )
    print(f"AST 大纲:可解析 {parsed} / 失败 {failed},给出 {symbols} 个符号条目")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
