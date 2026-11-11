"""The command line: init loads destinations; publish sends through the real Bot API adapter (HTTP mocked);
a destination without a token fails clearly instead of hanging in 'sending'."""
import json

import httpx
import respx

from orchestra import cli
from orchestra.approvals import decide, store_package
from orchestra.assets import AssetStore
from orchestra.db import PublishJob, make_engine, make_sessionmaker
from orchestra.publishing.publisher import Publisher
from conftest import make_package


def test_init_publish_pause(tmp_path, monkeypatch, capsys):
    orgs = tmp_path / "orgs" / "pouyan"
    orgs.mkdir(parents=True)
    (orgs / "destinations.yaml").write_text(
        "destinations:\n"
        "  - {id: tg_test, platform: telegram, chat_id: '@t', secret_env: TG_TOKEN_FOR_TEST}\n"
        "  - {id: bale_test, platform: bale, chat_id: '@b', secret_env: BALE_TOKEN_NOT_SET}\n", encoding="utf-8")
    db = f"sqlite:///{tmp_path / 'app.db'}"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORCHESTRA_DATABASE_URL", db)
    monkeypatch.setenv("ORCHESTRA_ASSET_DIR", str(tmp_path / "assets"))
    monkeypatch.setenv("ORCHESTRA_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.setenv("TG_TOKEN_FOR_TEST", "123:abc")
    monkeypatch.delenv("BALE_TOKEN_NOT_SET", raising=False)

    assert cli.main(["init"]) == 0
    assert "2 مقصد" in capsys.readouterr().out

    sessions = make_sessionmaker(make_engine(db))
    assets = AssetStore(tmp_path / "assets")
    pkg = make_package(assets)
    pub = Publisher(sessions, assets, adapter_factory=lambda d: None)
    with sessions.begin() as s:
        store_package(s, pkg)
        for dest in ("tg_test", "bale_test"):
            decide(s, pkg.package_id, pkg.package_hash, dest, "approved", "user:1")
            pub.enqueue(s, pkg.package_id, dest)

    assert cli.main(["pause"]) == 0
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("https://api.telegram.org/bot123:abc/sendMediaGroup").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": [{"message_id": 11}, {"message_id": 12}, {"message_id": 13}]}))
        cli.main(["publish"])
        assert not route.called  # paused: nothing leaves
        assert cli.main(["resume"]) == 0
        capsys.readouterr()
        cli.main(["publish"])
        out = capsys.readouterr().out
        assert route.call_count == 1
    results = json.loads(out.strip().splitlines()[-1])
    assert sorted(results.values()) == ["confirmed", "failed"]
    with sessions() as s:
        jobs = {j.destination_id: j for j in s.query(PublishJob)}
        assert jobs["tg_test"].message_ids == [11, 12, 13] and jobs["tg_test"].mode == "album"
        assert jobs["bale_test"].status == "failed" and "BALE_TOKEN_NOT_SET" in jobs["bale_test"].error
    cli.main(["jobs"])
    assert "confirmed" in capsys.readouterr().out


def test_imports_from_the_command_line(tmp_path, monkeypatch, capsys):
    from orchestra.db import DataSnapshot
    from orchestra.workflows.request import price_source, statements_source
    from test_specialists import STATEMENTS, synthetic_csv
    db = f"sqlite:///{tmp_path / 'app.db'}"
    monkeypatch.chdir(tmp_path)
    (tmp_path / "orgs" / "pouyan").mkdir(parents=True)
    (tmp_path / "orgs" / "pouyan" / "destinations.yaml").write_text("destinations: []\n", encoding="utf-8")
    monkeypatch.setenv("ORCHESTRA_DATABASE_URL", db)
    monkeypatch.setenv("ORCHESTRA_ASSET_DIR", str(tmp_path / "assets"))
    monkeypatch.setenv("ORCHESTRA_ORGS_DIR", str(tmp_path / "orgs"))
    cli.main(["init"])
    (tmp_path / "p.csv").write_bytes(synthetic_csv(40))
    (tmp_path / "s.yaml").write_bytes(STATEMENTS)
    assert cli.main(["import-prices", "p.csv", "--subject", "شاخص کل"]) == 0
    assert cli.main(["import-statements", "s.yaml"]) == 0
    assert "40 روز" in capsys.readouterr().out
    with make_sessionmaker(make_engine(db))() as s:
        sources = {x.source_id for x in s.query(DataSnapshot).all()}
    assert sources == {price_source("شاخص کل"), statements_source("نمونه")}
