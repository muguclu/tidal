import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from tidal.ingest.sources.eia import ROUTES

SCRIPT = Path(__file__).parents[3] / "scripts" / "verify_eia.py"
Handler = Callable[[httpx.Request], httpx.Response]
KEY = "SUPERSECRETKEY123"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_eia", SCRIPT)
    assert spec
    assert spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def handler(process: str = "PF4", facets: tuple[str, ...] = ("series",)) -> Handler:
    def h(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        if request.url.path.endswith("/data/"):
            s = p["facets[series][]"]
            rows = [{"period": "2020-01-02", "series": s, "process": process, "value": "1"}]
            return httpx.Response(200, json={"response": {"total": "1", "data": rows}})
        meta = {"facets": [{"id": f} for f in facets], "frequency": [{"id": "daily"}]}
        return httpx.Response(200, json={"response": meta})

    return h


def test_load_key_env_and_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = load()
    monkeypatch.delenv(m.KEY_ENV, raising=False)
    env = tmp_path / ".env"
    assert m.load_key(env) is None
    env.write_text(f"X=1\n{m.KEY_ENV}='{KEY}'\n")
    assert m.load_key(env) == KEY
    monkeypatch.setenv(m.KEY_ENV, "fromenv")
    assert m.load_key(env) == "fromenv"


def test_verify_ok_and_record_strips_key(tmp_path: Path) -> None:
    m = load()
    client = httpx.Client(transport=httpx.MockTransport(handler()))
    assert m.verify(client, KEY, tmp_path) == []
    files = list(tmp_path.glob("*.json"))
    assert len(files) >= len(ROUTES) * 2
    assert all(KEY not in f.read_text() for f in files)
    assert "url" in json.loads(files[0].read_text())


def test_verify_reports_mismatches() -> None:
    m = load()
    client = httpx.Client(transport=httpx.MockTransport(handler(facets=("process",))))
    assert any("no 'series' facet" in p for p in m.verify(client, KEY))
    down = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403)))
    problems = m.verify(down, KEY)
    assert problems
    assert all(KEY not in p for p in problems)


def test_main_without_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = load()
    monkeypatch.delenv(m.KEY_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert m.main([]) == 2
