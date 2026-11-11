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
    assert cli._destinations(cfg, "pouyan") == [["tg_test", "telegram"]]
