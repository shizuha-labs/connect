import importlib.util
import os
import sys
from pathlib import Path


def load_server(monkeypatch):
    repo = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("SHIZUHA_CONNECT_URL", "http://connect.test")
    sys.path.insert(0, str(repo / "mcp-server"))
    spec = importlib.util.spec_from_file_location("connect_server_under_test", repo / "mcp-server" / "connect_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_create_and_update_education_send_backend_field_names(monkeypatch):
    server = load_server(monkeypatch)
    calls = []

    def fake_request(method, path, data=None):
        calls.append((method, path, data))
        return {
            "id": "edu-1",
            "degree": data.get("degree", "BS"),
            "school_name": data.get("school_name", "Test University"),
        }

    monkeypatch.setattr(server.base, "api_request", fake_request)

    create_result = server.create_education("Test University", "BS", "2020-01-01")
    update_result = server.update_education("edu-1", school="Renamed University")

    assert calls[0] == (
        "POST",
        "/education/",
        {"school_name": "Test University", "degree": "BS", "start_date": "2020-01-01"},
    )
    assert calls[1] == ("PATCH", "/education/edu-1/", {"school_name": "Renamed University"})
    assert "BS at Test University" in create_result
    assert "Renamed University" in update_result


def test_create_and_update_experience_send_backend_field_names(monkeypatch):
    server = load_server(monkeypatch)
    calls = []

    def fake_request(method, path, data=None):
        calls.append((method, path, data))
        return {
            "id": "exp-1",
            "title": data.get("title", "Engineer"),
            "company_name": data.get("company_name", "Shizuha"),
        }

    monkeypatch.setattr(server.base, "api_request", fake_request)

    create_result = server.create_experience("Engineer", "Shizuha", "2024-01-01")
    update_result = server.update_experience("exp-1", company="Shizuha Labs")

    assert calls[0] == (
        "POST",
        "/experience/",
        {
            "title": "Engineer",
            "company_name": "Shizuha",
            "start_date": "2024-01-01",
            "is_current": False,
        },
    )
    assert calls[1] == ("PATCH", "/experience/exp-1/", {"company_name": "Shizuha Labs"})
    assert "Engineer at Shizuha" in create_result
    assert "Shizuha Labs" in update_result


def test_create_story_sends_text_story_contract(monkeypatch):
    server = load_server(monkeypatch)
    calls = []

    def fake_request(method, path, data=None):
        calls.append((method, path, data))
        return {"id": "story-1"}

    monkeypatch.setattr(server.base, "api_request", fake_request)

    result = server.create_story(content="hello", background_color="#112233")

    assert calls == [
        (
            "POST",
            "/stories/",
            {"story_type": "text", "text_content": "hello", "background_color": "#112233"},
        )
    ]
    assert "story-1" in result
