import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("HELIOS_API_TOKEN", "test-token-for-unit-tests")

from fastapi.testclient import TestClient
import cloud_app

client = TestClient(cloud_app.app)


def test_health_does_not_claim_scientific_validity():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert "No evidence of model validity" in body["scientific_status"]


def test_backtest_requires_bearer_token():
    response = client.post("/run", files={"panel": ("panel.csv", b"a\n1\n"), "daily": ("daily.csv", b"a\n1\n")})
    assert response.status_code == 401


def test_panel_builder_requires_bearer_token():
    response = client.post("/build-panel", files={"seasons": ("seasons.csv", b"a\n1\n"), "daily": ("daily.csv", b"a\n1\n")})
    assert response.status_code == 401


def test_short_or_missing_token_is_never_accepted(monkeypatch):
    monkeypatch.setattr(cloud_app, "TOKEN", "short")
    r = client.post("/run", headers={"Authorization": "Bearer short"},
                    files={"panel": ("p.csv", b"a\n1\n"), "daily": ("d.csv", b"a\n1\n")})
    assert r.status_code == 401
    assert client.get("/health").json()["auth_configured"] is False


def test_wrong_token_rejected_and_not_echoed():
    r = client.post("/run", headers={"Authorization": "Bearer wrong-token-value-xxxx"},
                    files={"panel": ("p.csv", b"a\n1\n"), "daily": ("d.csv", b"a\n1\n")})
    assert r.status_code == 401
    assert "wrong-token-value" not in r.text and "test-token-for-unit-tests" not in r.text


def test_bad_target_and_interaction_mode_rejected():
    h = {"Authorization": "Bearer test-token-for-unit-tests"}
    f = {"panel": ("p.csv", b"a\n1\n"), "daily": ("d.csv", b"a\n1\n")}
    assert client.post("/run", headers=h, files=f, data={"target": "--grid"}).status_code == 400
    assert client.post("/run", headers=h, files=f, data={"interaction_mode": "x"}).status_code == 400


def test_oversize_upload_rejected(monkeypatch):
    monkeypatch.setattr(cloud_app, "MAX_FILE_BYTES", 10)
    h = {"Authorization": "Bearer test-token-for-unit-tests"}
    r = client.post("/run", headers=h, files={"panel": ("p.csv", b"x" * 50), "daily": ("d.csv", b"a\n1\n")})
    assert r.status_code == 413


def test_bad_schema_returns_422_and_cleans_tempdirs():
    import glob, tempfile
    before = set(glob.glob(f"{tempfile.gettempdir()}/helios-*"))
    h = {"Authorization": "Bearer test-token-for-unit-tests"}
    r = client.post("/run", headers=h, files={"panel": ("p.csv", b"a\n1\n"), "daily": ("d.csv", b"a\n1\n")})
    assert r.status_code == 422
    assert set(glob.glob(f"{tempfile.gettempdir()}/helios-*")) == before
