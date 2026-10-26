import asyncio
import base64
import io
import json

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "MON_FILE", tmp_path / "monitors.json")
    monkeypatch.setattr(main, "list_runs", lambda: [])
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-provider-key")
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


def test_delete_returns_not_found_for_unknown_monitor(client):
    response = client.delete("/api/monitors/not-a-monitor")

    assert response.status_code == 404
    assert response.json()["detail"] == "Monitor was not found."


def test_monitor_rejects_blank_goal(client):
    response = client.post("/api/monitors", json={"goal": "   ", "minutes": 60})

    assert response.status_code == 422


def test_monitor_creation_requires_configured_provider(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")

    response = client.post("/api/monitors", json={"goal": "Check status", "minutes": 60})

    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["detail"]


def test_monitor_reports_alert_failure_without_losing_successful_run(monkeypatch):
    async def fake_execute(goal, headless):
        return {
            "id": "run-3",
            "ts": 3,
            "changed": True,
        }

    def fail_notify(_text):
        raise RuntimeError("Webhook unavailable")

    monkeypatch.setattr(main, "execute", fake_execute)
    monkeypatch.setattr(main, "notify", fail_notify)

    result = asyncio.run(
        main.run_monitor({"goal": "Check status"})
    )

    assert result["last_run"] == "run-3"
    assert result["last_changed"] is True
    assert result["error"] is None
    assert result["alert_error"] == "Webhook unavailable"


def test_healthcheck_reports_ready(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_status_reports_missing_model_configuration(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    response = client.get("/api/status")

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert response.json()["missing"] == ["OPENAI_API_KEY"]
    assert "OPENAI_API_KEY" in response.json()["message"]


def test_status_never_returns_model_credentials(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-value")

    response = client.get("/api/status")

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert "test-secret-value" not in response.text


def test_history_adds_local_report_link(client, monkeypatch):
    run = {"id": "run-1", "goal": "Check status", "summary": "Operational", "ts": 1, "changed": False}
    monkeypatch.setattr(main, "list_runs", lambda: [run])

    response = client.get("/api/history")

    assert response.status_code == 200
    assert response.json()[0]["report_url"] == "/reports/run-1/report.html"


def test_monitor_includes_latest_report_link(client, monkeypatch):
    main.save_mon(
        [
            {
                "id": "monitor-1",
                "goal": "Check status",
                "minutes": 60,
                "next": 10,
                "last_run": "run-1",
            }
        ]
    )
    monkeypatch.setattr(main, "BUCKET", None)

    response = client.get("/api/monitors")

    assert response.status_code == 200
    assert response.json()[0]["report_url"] == "/reports/run-1/report.html"


def test_report_escapes_goal_and_summary():
    report = main.render_report(
        "<script>alert(1)</script>",
        "<img src=x>",
        ["c2NyZWVuc2hvdA=="],
        ["https://status.example"],
    )

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in report
    assert "&lt;img src=x&gt;" in report
    assert 'src="step-1.png"' in report
    assert "data:image/png;base64" not in report
    assert "status.example" in report


def test_goal_matching_ignores_case_and_extra_whitespace():
    assert main.normalize_goal("  Check   AWS Status ") == main.normalize_goal("check aws status")


def test_archive_detects_changes_for_equivalent_goals(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "REPORTS", tmp_path)
    monkeypatch.setattr(main, "BUCKET", None)

    def screenshot(color):
        image = main.Image.new("RGB", (12, 12), color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode()

    previous = {
        "id": "20261026010000-a",
        "goal": "Check   payment status",
        "summary": "Service is operational",
        "ts": 1,
        "changed": False,
    }
    current = {
        "id": "20261026020000-b",
        "goal": " check payment STATUS ",
        "summary": "A service incident is active",
        "ts": 2,
        "changed": False,
    }

    main.archive(previous, [screenshot("white")], ["https://status.example"])
    main.archive(current, [screenshot("red")], ["https://status.example"])

    assert current["has_diff"] is True
    assert current["changed"] is True
    assert current["prev_id"] == previous["id"]
    assert (tmp_path / current["id"] / "report.html").exists()
    assert (tmp_path / current["id"] / "diff.png").exists()


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


def test_run_rejects_blank_goal(client):
    response = client.get("/api/run", params={"goal": "  "})

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "Goal must not be blank" in response.text


def test_run_limits_max_steps(client):
    response = client.get("/api/run", params={"goal": "Check status", "max_steps": 31})

    assert response.status_code == 422


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
