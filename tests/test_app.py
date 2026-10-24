import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "MON_FILE", tmp_path / "monitors.json")
    monkeypatch.setattr(main, "list_runs", lambda: [])
    with TestClient(main.app) as test_client:
        yield test_client


def test_homepage_serves_dashboard(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "Sentinel" in response.text
    assert "api/run" in response.text


def test_monitor_create_list_and_delete(client):
    response = client.post("/api/monitors", json={"goal": "Check status", "minutes": 1})

    assert response.status_code == 200
    monitor = response.json()[0]
    assert monitor["goal"] == "Check status"
    assert monitor["minutes"] == 5
    assert json.loads(main.MON_FILE.read_text())[0]["id"] == monitor["id"]
    assert client.get("/api/monitors").json() == [monitor]

    deleted = client.delete(f"/api/monitors/{monitor['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == []


def test_history_adds_local_report_link(client, monkeypatch):
    run = {"id": "run-1", "goal": "Check status", "summary": "Operational", "ts": 1, "changed": False}
    monkeypatch.setattr(main, "list_runs", lambda: [run])

    response = client.get("/api/history")

    assert response.status_code == 200
    assert response.json()[0]["report_url"] == "/reports/run-1/report.html"


def test_run_streams_completion_event(client, monkeypatch):
    async def fake_execute(goal, max_steps, on_step, headless=main.HEADLESS):
        return {
            "id": "run-2",
            "goal": goal,
            "summary": "Operational",
            "ts": 2,
            "changed": False,
        }

    monkeypatch.setattr(main, "execute", fake_execute)

    response = client.get("/api/run", params={"goal": "Check status"})

    assert response.status_code == 200
    assert 'event: done' in response.text
    assert '"summary": "Operational"' in response.text


def test_run_reports_missing_provider_key(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    response = client.get("/api/run", params={"goal": "Check status"})

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "OPENAI_API_KEY is required" in response.text


def test_execute_uses_browser_without_optional_extensions(monkeypatch):
    observed = {}

    class FakeBrowser:
        def __init__(self, **kwargs):
            observed["browser_options"] = kwargs

    class FakeResult:
        def screenshots(self):
            return []

        def urls(self):
            return []

        def final_result(self):
            return "Operational"

    class FakeAgent:
        def __init__(self, **kwargs):
            observed["agent_options"] = kwargs

        async def run(self, max_steps, on_step_end):
            return FakeResult()

    monkeypatch.setattr(main, "Browser", FakeBrowser)
    monkeypatch.setattr(main, "Agent", FakeAgent)
    monkeypatch.setattr(main, "make_llm", lambda: object())
    monkeypatch.setattr(main, "archive", lambda meta, shots, urls: None)

    result = asyncio.run(main.execute("Check status", headless=True))

    assert result["summary"] == "Operational"
    assert observed["browser_options"] == {
        "headless": True,
        "enable_default_extensions": False,
    }
    assert observed["agent_options"]["browser"] is not None


@pytest.mark.parametrize(
    ("provider", "key"),
    [
        ("openai", "OPENAI_API_KEY"),
        ("gemini", "GOOGLE_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
    ],
)
def test_provider_reports_missing_credentials(provider, key, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.delenv(key, raising=False)

    with pytest.raises(RuntimeError, match=key):
        main.make_llm()
