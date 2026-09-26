"""P3-3 守卫三链 + 单元测试:堵死 P0-2 复活链。

背景(R2-Q5):llm_enabled=true + 凭据齐 + PATCHPILOT_LLM_MODEL="" 的组合,
此前 API+graph 链全程零拦截,report.json 落盘 model_name=""——上轮 P0-2
「真模型评测不可追溯」被一条现役配置组合精确复活。

修法:守卫下沉 openai_client 构造(所有 build_model("openai") 入口共用)+
run_task_graph 补第四入口守卫。fake 豁免语义保留(夜志 E2:回放零花费,
service/run_single 的 fake 路径传空 model_name,不豁免会炸断回放链路)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import Settings, get_settings
from app.errors import TaskError
from app.llm.openai_client import build_model


def _real_settings(**overrides) -> Settings:
    """llm_enabled=true + 凭据齐的配置;llm_model 默认留空(复活链配置)。"""
    values: dict = {
        "llm_enabled": True,
        "llm_base_url": "http://localhost:9/v1",
        "llm_api_key": "test-key",
        "llm_model": "",
    }
    values.update(overrides)
    return Settings(**values)


# ---------- 单元:守卫下沉 openai_client 构造 ----------


def test_openai_client_rejects_empty_llm_model() -> None:
    """构造即校验:llm_model 空串/纯空白一律 TaskError(先于 openai 包导入)。"""
    with pytest.raises(TaskError, match="model name"):
        build_model("openai", _real_settings())
    with pytest.raises(TaskError, match="model name"):
        build_model("openai", _real_settings(llm_model="   "))


def test_fake_provider_unaffected_by_guard() -> None:
    """fake 豁免语义保留:llm_enabled=true 下 fake 回放照常构造(零花费路径)。"""
    from app.llm.fake import FakeLLM

    model = build_model("fake", _real_settings(), script=[{"tool": "finish", "args": {}}])
    assert isinstance(model, FakeLLM)


# ---------- 链 1/2:API + graph、API + plain(create_task 预检,404 零副作用) ----------


@pytest.mark.parametrize("engine", ["graph", "plain"])
def test_api_openai_empty_model_name_rejected_before_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    """复活链钉死:两条引擎链都在 create_task 预检失败(404),
    不建任务行、不置状态、不产生产物。"""
    patched = get_settings().model_copy(
        update={
            "llm_enabled": True,
            "llm_base_url": "http://localhost:9/v1",
            "llm_api_key": "test-key",
            "llm_model": "",
        }
    )
    monkeypatch.setattr("app.api.service.get_settings", lambda: patched)
    app = create_app(db_path=tmp_path / "db.sqlite3", runs_root=tmp_path / "runs")
    with TestClient(app) as client:
        resp = client.post(
            "/api/tasks", json={"bug_id": "BUG-001", "engine": engine, "model": "openai"}
        )
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["code"] == "invalid_task"
        assert "model name" in body["message"]
        # 零副作用:库里没有任何任务行
        assert client.get("/api/tasks").json()["tasks"] == []
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").iterdir())


# ---------- 链 3:run_single CLI ----------


def test_run_single_empty_model_name_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_single --model openai 在 llm_model="" 时构造期即拒,零产物落盘。"""
    from app.evals.run_single import main

    patched = get_settings().model_copy(
        update={
            "llm_enabled": True,
            "llm_base_url": "http://localhost:9/v1",
            "llm_api_key": "test-key",
            "llm_model": "",
        }
    )
    monkeypatch.setattr("app.evals.run_single.get_settings", lambda: patched)
    with pytest.raises(TaskError, match="model name"):
        main(["--bug", "BUG-001", "--model", "openai", "--out", str(tmp_path / "runs")])
    assert not (tmp_path / "runs").exists()


# ---------- 链 4(直连):run_task_graph 第四入口守卫 ----------


def test_run_task_graph_empty_model_name_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第四入口(run_task_graph)与 driver.run_task 同口径:真实 provider 缺
    model_name → ValueError,先于任何目录物化;fake-replay 豁免。"""
    from types import SimpleNamespace

    from app.evals.bugset import load_bug
    from app.graph.runner import run_task_graph

    patched = get_settings().model_copy(update={"llm_enabled": True})
    # runner 对 get_settings 是函数内懒加载(from app.config import ...),
    # 必须 patch 来源模块;patched 仅翻 llm_enabled,其余配置不变
    monkeypatch.setattr("app.config.get_settings", lambda: patched)
    bug = load_bug("BUG-001", Path("bugs"))
    real_stub = SimpleNamespace(provider="openai")  # 守卫先 raise,模型不参与执行
    with pytest.raises(ValueError, match="model_name"):
        run_task_graph(bug, real_stub, runs_root=tmp_path / "runs", model_name="")
    assert not (tmp_path / "runs").exists()

    # fake-replay 豁免:空 model_name 不拦(零花费回放路径),守卫放行后
    # 函数继续走正常执行流(stub 无 complete → 收敛 NEEDS_REVIEW,非守卫拒绝)
    fake_stub = SimpleNamespace(provider="fake-replay")
    result = run_task_graph(bug, fake_stub, runs_root=tmp_path / "runs2", model_name="")
    assert (tmp_path / "runs2").exists()  # 守卫放行:目录已物化
    assert result.status == "NEEDS_REVIEW"  # 执行期收敛,不是发起期拒绝
