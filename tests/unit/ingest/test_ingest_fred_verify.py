import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import httpx
import pytest

SCRIPT = Path(__file__).parents[3] / "scripts" / "verify_fred.py"
Handler = Callable[[httpx.Request], httpx.Response]
KEY = "SUPERSECRETKEY123"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_fred", SCRIPT)
    assert spec
    assert spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def handler(first: str = "1999-01-04", freq: str = "D") -> Handler:
    def h(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        if request.url.path.endswith("/series"):
            meta = {"id": p["series_id"], "frequency_short": freq, "observation_start": first}
            return httpx.Response(200, json={"seriess": [meta]})
        day = first if p["sort_order"] == "asc" else "2026-10-20"
        rows = [{"date": day, "value": "1.1"}]
        return httpx.Response(200, json={"count": 7000, "observations": rows})

    return h


def test_load_key_env_and_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = load()
    monkeypatch.delenv(m.KEY_ENV, raising=False)
    env = tmp_path / ".env"
    assert m.load_key(env) is None
    env.write_text(f"X=1\n{m.KEY_ENV}='{KEY}'\n", encoding="utf-8")
    assert m.load_key(env) == KEY
    monkeypatch.setenv(m.KEY_ENV, "fromenv")
    assert m.load_key(env) == "fromenv"


def test_verify_ok_and_record_strips_key(tmp_path: Path) -> None:
    m = load()
    client = httpx.Client(transport=httpx.MockTransport(handler()))
    assert m.verify(client, KEY, tmp_path) == []
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 3
    assert all(KEY not in f.read_text(encoding="utf-8") for f in files)
    assert "url" in json.loads(files[0].read_text(encoding="utf-8"))


def test_verify_reports_mismatches() -> None:
    m = load()
    client = httpx.Client(transport=httpx.MockTransport(handler(first="1999-01-05", freq="W")))
    problems = m.verify(client, KEY)
    assert any("first observation" in p for p in problems)
    assert any("frequency" in p for p in problems)
    down = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403)))
    problems = m.verify(down, KEY)
    assert problems
    assert all(KEY not in p for p in problems)


def test_main_without_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = load()
    monkeypatch.delenv(m.KEY_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert m.main([]) == 2


def test_main_exit_codes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = load()
    monkeypatch.setenv(m.KEY_ENV, KEY)
    monkeypatch.chdir(tmp_path)
    real = httpx.Client

    def good(**kw: object) -> httpx.Client:
        return real(transport=httpx.MockTransport(handler()))

    def bad(**kw: object) -> httpx.Client:
        return real(transport=httpx.MockTransport(handler(freq="M")))

    monkeypatch.setattr(m.httpx, "Client", good)
    assert m.main([]) == 0
    monkeypatch.setattr(m.httpx, "Client", bad)
    assert m.main(["--record"]) == 1


def test_transport_error_is_a_mismatch_not_a_crash() -> None:
    m = load()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    problems = m.verify(httpx.Client(transport=httpx.MockTransport(boom)), KEY)
    assert problems
    assert all(KEY not in p for p in problems)
