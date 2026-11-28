"""DBOS needs every step and workflow name to be unique across all workflow modules; a clash makes a restarted run
resume the wrong function."""
import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "orchestra" / "workflows"


def test_dbos_function_names_are_unique():
    names = []
    for path in ROOT.glob("*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any("DBOS" in ast.unparse(d) for d in node.decorator_list):
                names.append(node.name)
    assert [n for n, c in Counter(names).items() if c > 1] == []


def test_cli_prepares_a_fresh_database(tmp_path, monkeypatch):
    from orchestra import cli
    from orchestra.settings import load_settings
    orgs = tmp_path / "orgs" / "pouyan"
    orgs.mkdir(parents=True)
    (orgs / "destinations.yaml").write_text("destinations:\n  - {id: tg_test, platform: telegram, chat_id: '@t', secret_env: X}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORCHESTRA_DATABASE_URL", f"sqlite:///{tmp_path / 'fresh.db'}")
    monkeypatch.setenv("ORCHESTRA_ORGS_DIR", str(tmp_path / "orgs"))
    cfg = load_settings()
    cli._ensure_ready(cfg, "pouyan")  # no `init` was run before
    assert cli._destinations(cfg, "pouyan") == [["tg_test", "telegram", "post"]]


# ---------------------------------------------------------------- LLM settings and re-runs

def _env(monkeypatch, tmp_path, **kw):
    from orchestra.settings import load_settings
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORCHESTRA_DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")
    for k in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_FALLBACK_MODEL"):
        monkeypatch.delenv(f"ORCHESTRA_{k}", raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(f"ORCHESTRA_{k}", v)
    return load_settings()


def test_model_list_becomes_a_fallback_chain(tmp_path, monkeypatch):
    from orchestra import cli
    cfg = _env(monkeypatch, tmp_path, LLM_PROVIDER="openai_compatible", LLM_API_KEY="k", LLM_MODEL="m1, m2,m3",
               LLM_BASE_URL="https://example.com/v1/")
    routes = cli._runtime(cfg).gateway.tiers["writer"]
    assert [r.model for r in routes] == ["m1", "m2", "m3"]


def test_bad_settings_are_explained_before_any_call(tmp_path, monkeypatch):
    import pytest
    from orchestra import cli
    cfg = _env(monkeypatch, tmp_path, LLM_PROVIDER="openai_compatible", LLM_API_KEY="k", LLM_MODEL="gemini-x")
    with pytest.raises(SystemExit, match="https://") as e:
        cli._runtime(cfg)
    assert "generativelanguage" in str(e.value)  # a Gemini model gets its address as a hint
    cfg = _env(monkeypatch, tmp_path, LLM_API_KEY="k", LLM_MODEL="gemini-x")  # provider left at anthropic
    with pytest.raises(SystemExit, match="openai_compatible"):
        cli._runtime(cfg)


def test_provider_reports_a_bad_address_as_not_retryable():
    import asyncio

    import pytest
    from orchestra.llm import LLMError, OpenAICompatibleProvider
    with pytest.raises(LLMError, match="https://") as e:
        asyncio.run(OpenAICompatibleProvider("k", "generativelanguage.googleapis.com").complete("m", "s", "u", 10))
    assert e.value.retryable is False


def test_failed_run_does_not_block_a_corrected_rerun(tmp_path):
    from orchestra.db import Base, Run, make_engine, make_sessionmaker
    from orchestra.workflows.request import fresh_run_id
    engine = make_engine(f"sqlite:///{tmp_path / 'r.db'}")
    Base.metadata.create_all(engine)
    sessions = make_sessionmaker(engine)
    assert fresh_run_id(sessions, "a") == "a"
    with sessions.begin() as s:
        s.add(Run(id="a", org_id="o", template="t", status="failed"))
    assert fresh_run_id(sessions, "a") == "a:r1"
    with sessions.begin() as s:
        s.add(Run(id="a:r1", org_id="o", template="t", status="awaiting_approval"))
    assert fresh_run_id(sessions, "a") == "a:r1"  # a run waiting for approval is reused, never duplicated


def test_unreachable_model_service_fails_fast_with_a_clear_message(tmp_path, monkeypatch):
    import httpx
    import pytest
    import respx
    from orchestra import cli
    cfg = _env(monkeypatch, tmp_path, LLM_PROVIDER="openai_compatible", LLM_API_KEY="k", LLM_MODEL="m",
               LLM_BASE_URL="https://llm.example/v1/")
    with respx.mock() as router:
        router.get("https://llm.example/v1/").mock(side_effect=httpx.ConnectTimeout("t"))
        with pytest.raises(SystemExit, match="VPN"):
            cli._check_llm_reachable(cfg)
    with respx.mock() as router:  # any HTTP answer, even 404, means the service is reachable
        router.get("https://llm.example/v1/").mock(return_value=httpx.Response(404))
        cli._check_llm_reachable(cfg)


def test_new_attempt_even_when_earlier_one_waits(tmp_path):
    from orchestra.db import Base, Run, make_engine, make_sessionmaker
    from orchestra.workflows.request import fresh_run_id
    engine = make_engine(f"sqlite:///{tmp_path / 'n.db'}")
    Base.metadata.create_all(engine)
    sessions = make_sessionmaker(engine)
    with sessions.begin() as s:
        s.add(Run(id="a", org_id="o", template="t", status="awaiting_approval"))
    assert fresh_run_id(sessions, "a") == "a"
    assert fresh_run_id(sessions, "a", new=True) == "a:r1"


def test_ask_accepts_new_flag(monkeypatch, tmp_path):
    from orchestra import cli
    monkeypatch.setenv("ORCHESTRA_DATABASE_URL", f"sqlite:///{tmp_path / 'a.db'}")
    seen = {}
    monkeypatch.setattr(cli, "cmd_ask", lambda cfg, a: seen.update(new=a.new, request=a.request))
    cli.main(["ask", "--new", "سلام"])
    assert seen == {"new": True, "request": "سلام"}


def test_openai_compatible_asks_for_room_for_hidden_reasoning():
    import asyncio
    import json as _json

    import httpx
    import respx

    from orchestra.llm import OpenAICompatibleProvider
    with respx.mock() as router:
        route = router.post("https://llm.example/v1/chat/completions").mock(return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}))
        asyncio.run(OpenAICompatibleProvider("k", "https://llm.example/v1/").complete("m", "s", "u", 800))
        assert _json.loads(route.calls[0].request.content)["max_tokens"] >= 8192


def test_an_existing_database_gets_the_new_destination_column(tmp_path):
    import sqlite3

    from orchestra.db import create_schema, make_engine
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)  # the shape a database created before the column existed has
    con.execute("CREATE TABLE destinations (id VARCHAR(64) PRIMARY KEY, org_id VARCHAR(64), platform VARCHAR(16), "
                "chat_id VARCHAR(128), secret_env VARCHAR(128), enabled BOOLEAN, created_at VARCHAR)")
    con.execute("INSERT INTO destinations VALUES ('tg', 'o', 'telegram', '@c', 'X', 1, '2026-01-01')")
    con.commit()
    con.close()
    create_schema(make_engine(f"sqlite:///{path}"))
    create_schema(make_engine(f"sqlite:///{path}"))  # twice: nothing to add the second time
    assert sqlite3.connect(path).execute("select content from destinations").fetchone() == ("post",)


def test_a_malformed_model_name_is_explained_at_start(tmp_path, monkeypatch):
    from orchestra.cli import _runtime
    from orchestra.settings import Settings
    cfg = Settings(llm_api_key="k", llm_model="صw", llm_provider="openai_compatible",
                   llm_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                   database_url=f"sqlite:///{tmp_path / 'a.db'}")
    import pytest
    with pytest.raises(SystemExit) as e:
        _runtime(cfg)
    assert "صw" in str(e.value) and "setup.bat" in str(e.value)
