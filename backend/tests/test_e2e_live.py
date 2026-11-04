"""Phase 4 Stage 4: the scripts/e2e_check.py scenarios, run against a real uvicorn server
(127.0.0.1, free port) over HTTP + SSE. Mock tools and synthetic fault injection only."""

import pytest

from scripts.e2e_check import SCENARIOS, CheckFailed, live_server


@pytest.fixture(scope="module")
def live():
    with live_server() as server:
        yield server


@pytest.mark.parametrize(("name", "scenario"), SCENARIOS, ids=[name for name, _ in SCENARIOS])
def test_e2e_scenario(live, name, scenario):
    try:
        scenario(live)
    except CheckFailed as exc:
        pytest.fail(f"{name}: {exc}")
