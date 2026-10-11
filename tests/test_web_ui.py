import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from decinav.web_ui import ArchitectureUIService, UIError, create_ui_server
from test_intelligence import setup_architecture_project


FIXTURES = Path(__file__).parent / "fixtures"


def test_ui_status_exposes_repository_health_and_nonclickable_graph(
    tmp_path: Path,
) -> None:
    setup_architecture_project(tmp_path)
    service = ArchitectureUIService(tmp_path)

    status = service.status()

    assert status["initialized"] is True
    assert status["valid"] is True
    assert status["adr_count"] == 2
    assert [record["id"] for record in status["records"]] == [
        "ADR-0001",
        "ADR-0002",
    ]
    assert "ADR_0001 -->" in status["mermaid"]
    assert "click ADR_0001" not in status["mermaid"]


def test_ui_dispatches_impact_and_query_through_purpose_specific_mocks(
    tmp_path: Path, monkeypatch
) -> None:
    setup_architecture_project(tmp_path)
    service = ArchitectureUIService(tmp_path)
    monkeypatch.setenv("DECINAV_AI_CMD", "mock")
    monkeypatch.setenv(
        "DECINAV_AI_MOCK_IMPACT_RESPONSE", str(FIXTURES / "mock_impact.json")
    )
    monkeypatch.setenv(
        "DECINAV_AI_MOCK_QUERY_RESPONSE", str(FIXTURES / "mock_query.txt")
    )

    impact = service.dispatch(
        "impact",
        {
            "project": str(tmp_path),
            "prd_name": "live-results.md",
            "prd_text": "# Live results\n\nReturn evaluation synchronously.\n",
        },
    )
    query = service.dispatch(
        "query",
        {"project": str(tmp_path), "question": "Where does evaluation execute?"},
    )

    assert impact["directly_affected_adrs"][0]["id"] == "ADR-0002"
    assert "classDef affected" in impact["mermaid"]
    assert "[ADR-0002]" in query["answer"]


def test_ui_rejects_non_loopback_binding(tmp_path: Path) -> None:
    with pytest.raises(UIError, match="only binds to a loopback"):
        create_ui_server(tmp_path, host="0.0.0.0", port=0)


def test_http_ui_serves_asset_and_requires_request_token(tmp_path: Path) -> None:
    try:
        info = create_ui_server(tmp_path, port=0)
    except PermissionError:
        pytest.skip("test sandbox does not permit binding a loopback socket")
    thread = Thread(target=info.server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(info.url, timeout=2) as response:
            html = response.read().decode("utf-8")
        assert "Decinav" in html
        assert info.token in html

        with pytest.raises(HTTPError) as error:
            urlopen(info.url + "api/status", timeout=2)
        assert error.value.code == 403

        request = Request(
            info.url + "api/status",
            headers={"X-Decinav-UI-Token": info.token},
        )
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read())
        assert payload["project"] == str(tmp_path)
        assert payload["initialized"] is False
    finally:
        info.server.shutdown()
        info.server.server_close()
        thread.join(timeout=2)


def test_http_ui_serves_allowlisted_static_assets_only(tmp_path: Path) -> None:
    try:
        info = create_ui_server(tmp_path, port=0)
    except PermissionError:
        pytest.skip("test sandbox does not permit binding a loopback socket")
    thread = Thread(target=info.server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(info.url, timeout=2) as response:
            html = response.read().decode("utf-8")
        assert '<script type="application/json" id="decinav-ui-config">{"token"' in html
        assert info.token in html
        assert 'src="/static/app.js"' in html
        assert 'src="/static/vendor/dagre.min.js"' in html
        assert 'href="/static/app.css"' in html

        expected_types = {
            "static/app.js": "text/javascript; charset=utf-8",
            "static/app.css": "text/css; charset=utf-8",
            "static/vendor/dagre.min.js": "text/javascript; charset=utf-8",
        }
        for path, content_type in expected_types.items():
            with urlopen(info.url + path, timeout=2) as response:
                body = response.read()
                assert response.status == 200
                assert response.headers["Content-Type"] == content_type
                assert response.headers["X-Content-Type-Options"] == "nosniff"
                assert len(body) > 1000
        with urlopen(info.url + "static/vendor/dagre.min.js", timeout=2) as response:
            assert b"dagre" in response.read()[:200]

        for forbidden in (
            "static/../web_ui.py",
            "static/%2e%2e/web_ui.py",
            "static/index.html",
            "static/vendor/LICENSE-dagre.txt",
            "static/",
            "static/nope.js",
        ):
            with pytest.raises(HTTPError) as error:
                urlopen(info.url + forbidden, timeout=2)
            assert error.value.code == 404, forbidden
    finally:
        info.server.shutdown()
        info.server.server_close()
        thread.join(timeout=2)


def test_ui_page_has_compact_header_and_theme_toggle(tmp_path: Path) -> None:
    try:
        info = create_ui_server(tmp_path, port=0)
    except PermissionError:
        pytest.skip("test sandbox does not permit binding a loopback socket")
    thread = Thread(target=info.server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(info.url, timeout=2) as response:
            html = response.read().decode("utf-8")
        assert 'id="repo-chip"' in html
        assert 'id="theme-toggle"' in html
        assert 'id="project-switch" hidden' in html
        assert 'id="stat-ai"' in html
        assert "onclick=" not in html
        with urlopen(info.url + "static/app.css", timeout=2) as response:
            css = response.read().decode("utf-8")
        assert ':root[data-theme="dark"]' in css
        assert "prefers-reduced-motion" in css
    finally:
        info.server.shutdown()
        info.server.server_close()
        thread.join(timeout=2)
