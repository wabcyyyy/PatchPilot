"""S01/F3:测试身份解析与 JUnit 匹配的单元矩阵。

复现缺陷(PatchPilot-review.md F3):请求 `file::TestA::test_same` 时只有
`TestB::test_same` 通过也算 all_passed——旧匹配只对 file+方法名,类链被丢弃。
本文件把"什么算同一条测试身份"逐形态钉死;经真实 pytest 执行的端到端用例
在 tests/test_executor.py 与 tests/test_custom_task.py。
"""

from __future__ import annotations

import pytest

from app.adapters.test_identity import case_matches, parse_node_id, validate_concrete_node_id


def _match(requested: str, file_attr: str, classname: str, name: str) -> bool:
    identity = parse_node_id(requested)
    assert identity is not None, f"fixture bug: {requested!r} must parse"
    return case_matches(identity, file_attr, classname, name)


# ---------- 解析 ----------


@pytest.mark.parametrize(
    ("node_id", "file", "chain", "func", "params"),
    [
        ("tests/test_x.py::test_same", "tests/test_x.py", (), "test_same", None),
        (
            "tests/test_x.py::TestA::test_same",
            "tests/test_x.py",
            ("TestA",),
            "test_same",
            None,
        ),
        (
            "pkg/sub/test_x.py::TestOuter::TestInner::test_deep",
            "pkg/sub/test_x.py",
            ("TestOuter", "TestInner"),
            "test_deep",
            None,
        ),
        (
            "tests/test_tuple.py::test_tuple[(1,2)]",
            "tests/test_tuple.py",
            (),
            "test_tuple",
            "(1,2)",
        ),
        (
            "tests/test_x.py::TestA::test_same[a b]",
            "tests/test_x.py",
            ("TestA",),
            "test_same",
            "a b",
        ),
    ],
)
def test_parse_concrete_ids(node_id: str, file: str, chain: tuple, func: str, params: str) -> None:
    identity = parse_node_id(node_id)
    assert identity is not None
    assert identity.file_path == file
    assert identity.class_chain == chain
    assert identity.function == func
    assert identity.params == params
    assert identity.case_name == (f"{func}[{params}]" if params is not None else func)


@pytest.mark.parametrize(
    "node_id",
    [
        "",  # 空
        "tests/test_x.py",  # 文件 selector(不具体)
        "src/",  # 目录 selector
        "tests/test_x.py::TestA",  # 仅类 selector
        "tests/test_x.py::helper",  # 末段不是 test* 函数
        "tests/test_x.py::test_a::test_b",  # 中间段不是类
        "tests/test_x.py::test_a[",  # 参数段不闭合
        "tests/test_x.py::test_a[]",  # 参数段为空
        "../evil.py::test_a",  # 路径逃逸
        "tests/../../evil.py::test_a",
        "/abs/test_x.py::test_a",  # 绝对路径
        "//host/share/test_x.py::test_a",  # UNC
        "C:/evil/test_x.py::test_a",  # 盘符
        "tests//test_x.py::test_a",  # 空路径段
        "tests\\test_x.py::test_a",  # 反斜杠(请求 id 一律 POSIX)
        " tests/test_x.py::test_a",  # 前导空白
        "tests/test_x.py::test_a;ls",  # 参数段/正文 shell 元字符
        "tests/test_x.py::test_a$(cmd)",  # 命令替换
        "tests/test_x.py::test_a\n-p no:cov",  # 换行注入
    ],
)
def test_parse_rejects_non_concrete_or_unsafe(node_id: str) -> None:
    assert parse_node_id(node_id) is None


def test_parse_param_allows_data_characters_but_not_injection() -> None:
    # 引号/逗号/空格/等号是合法数据;括号内的 :: 与 . 不当边界
    identity = parse_node_id("tests/test_x.py::test_eq[x='::'']")
    assert identity is not None
    assert identity.params == "x='::''" or identity.params.startswith("x=")
    assert identity.function == "test_eq"


# ---------- 匹配:同名不互认 ----------


def test_f3_same_name_classes_do_not_cross_match() -> None:
    """F3 核心:请求 TestA,只有 TestB 通过 → 不匹配(旧实现假通过)。"""
    assert (
        _match(
            "tests/test_x.py::TestA::test_same",
            "tests/test_x.py",
            "tests.test_x.TestB",
            "test_same",
        )
        is False
    )
    assert (
        _match(
            "tests/test_x.py::TestA::test_same",
            "tests/test_x.py",
            "tests.test_x.TestA",
            "test_same",
        )
        is True
    )


def test_module_function_and_class_method_do_not_substitute() -> None:
    """模块函数与类方法互相顶替一律拒绝(双向)。"""
    # 请求模块函数,junit 是同名类方法
    assert (
        _match("tests/test_x.py::test_same", "tests/test_x.py", "tests.test_x.TestA", "test_same")
        is False
    )
    # 请求类方法,junit 是同名模块函数
    assert (
        _match("tests/test_x.py::TestA::test_same", "tests/test_x.py", "tests.test_x", "test_same")
        is False
    )


def test_nested_class_chain_must_be_complete() -> None:
    assert _match(
        "tests/test_x.py::TestOuter::TestInner::test_deep",
        "tests/test_x.py",
        "tests.test_x.TestOuter.TestInner",
        "test_deep",
    )
    # 类链缺一段/换兄弟类:不匹配
    assert not _match(
        "tests/test_x.py::TestOuter::TestInner::test_deep",
        "tests/test_x.py",
        "tests.test_x.TestOuter",
        "test_deep",
    )
    assert not _match(
        "tests/test_x.py::TestOuter::TestInner::test_deep",
        "tests/test_x.py",
        "tests.test_x.TestOuter.TestOther.TestInner",
        "test_deep",
    )


def test_parametrized_id_is_exact() -> None:
    assert _match(
        "tests/test_x.py::test_x[(1,2)]", "tests/test_x.py", "tests.test_x", "test_x[(1,2)]"
    )
    assert not _match(
        "tests/test_x.py::test_x[(1,2)]", "tests/test_x.py", "tests.test_x", "test_x[(1,3)]"
    )
    assert not _match("tests/test_x.py::test_x[(1,2)]", "tests/test_x.py", "tests.test_x", "test_x")
    # 参数段内的 - 不是文件边界,同名前缀也不互认
    assert not _match(
        "tests/test_x.py::test_x[a-b]", "tests/test_x.py", "tests.test_x", "test_x[a]"
    )


# ---------- file 缺失与 rootdir ----------


def test_missing_file_attr_falls_back_to_classname_tail() -> None:
    """无 file 属性:按点分模块路径+类链核对,不同类不互认。"""
    assert _match("tests/test_x.py::TestA::test_same", "", "tests.test_x.TestA", "test_same")
    assert not _match("tests/test_x.py::TestA::test_same", "", "tests.test_x.TestB", "test_same")
    assert _match("tests/test_x.py::test_same", "", "tests.test_x", "test_same")


def test_rootdir_change_still_matches_by_suffix() -> None:
    """rootdir 内移:classname 前缀变化,后缀核对仍命中同一文件+类链。"""
    assert _match(
        "tests/test_x.py::TestA::test_same",
        "tests/test_x.py",
        "src.pkg.tests.test_x.TestA",
        "test_same",
    )


def test_windows_separator_in_junit_file_attr() -> None:
    """junit 在 Windows 上偶发反斜杠 file 属性:归一后仍匹配。"""
    assert _match("tests/test_x.py::test_same", "tests\\test_x.py", "tests.test_x", "test_same")


def test_same_suffix_file_requires_exact_or_slash_boundary() -> None:
    """后缀匹配按路径段边界:othertests/ 不得顶替 tests/。"""
    assert not _match(
        "tests/test_x.py::test_same", "othertests/test_x.py", "tests.test_x", "test_same"
    )


# ---------- 预检消息分层 ----------


def test_validate_preserves_escape_messages() -> None:
    for tid in ("../evil.py::test_t", "C:/evil/test_x.py::test_t", "//host/s/test_x.py::test_t"):
        with pytest.raises(ValueError, match=r"escapes the workspace|workspace-relative"):
            validate_concrete_node_id(tid)
    with pytest.raises(ValueError, match="not a concrete node id"):
        validate_concrete_node_id("tests/test_x.py")
    with pytest.raises(ValueError, match="selects a class"):
        validate_concrete_node_id("tests/test_x.py::TestA")
