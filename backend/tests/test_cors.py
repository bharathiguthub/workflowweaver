"""Phase 4 Stage 3: development CORS for the React frontend is restricted to exact origins."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import DEV_FRONTEND_ORIGINS, Settings, cors_origins
from app.main import app


def preflight(origin: str, method: str = "POST", headers: str = "content-type"):
    return TestClient(app).options(
        "/api/v1/workflows/plan",
        headers={"Origin": origin, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": headers},
    )


def test_default_development_origins_are_the_vite_dev_server_only():
    assert cors_origins(Settings(_env_file=None)) == list(DEV_FRONTEND_ORIGINS)


def test_no_default_origins_outside_development():
    assert cors_origins(Settings(_env_file=None, app_env="production")) == []
    assert cors_origins(Settings(_env_file=None, app_env="staging")) == []


def test_explicit_origins_override_defaults():
    settings = Settings(_env_file=None, app_env="production", cors_allow_origins=" https://ui.example.invalid/ ,http://localhost:3000")
    assert cors_origins(settings) == ["https://ui.example.invalid", "http://localhost:3000"]


def test_wildcard_is_rejected():
    with pytest.raises(ValueError):
        cors_origins(Settings(_env_file=None, cors_allow_origins="*"))


@pytest.mark.skipif(not cors_origins(Settings()), reason="CORS disabled by the local configuration")
def test_vite_origin_preflight_allowed():
    response = preflight("http://127.0.0.1:5173")
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert "access-control-allow-credentials" not in response.headers


def test_other_origin_preflight_rejected():
    response = preflight("http://evil.example.invalid")
    assert response.headers.get("access-control-allow-origin") is None


@pytest.mark.skipif(not cors_origins(Settings()), reason="CORS disabled by the local configuration")
def test_disallowed_method_and_header_rejected():
    assert preflight("http://127.0.0.1:5173", method="DELETE").status_code == 400
    assert preflight("http://127.0.0.1:5173", headers="authorization").status_code == 400
