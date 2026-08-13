"""Morning brief composition + chain source trail."""

import pytest


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from shunkan.server.api import create_app

    return TestClient(create_app())


def test_brief_composes_all_sections(client):
    d = client.get("/api/brief/NIFTY").json()
    assert d["symbol"] == "NIFTY"
    for key in ("cues", "positioning", "vol", "kalman", "fan", "analogs",
                "votes", "net"):
        assert key in d, f"missing section {key}"
    assert len(d["votes"]) >= 4
    for v in d["votes"]:
        assert v["dir"] in ("bullish", "bearish", "neutral")
        assert v["why"]


def test_brief_net_score_matches_votes(client):
    d = client.get("/api/brief/NIFTY").json()
    score = sum(1 if v["dir"] == "bullish" else -1 if v["dir"] == "bearish" else 0
                for v in d["votes"])
    assert d["net"]["score"] == score
    assert d["net"]["prov"]["caveat"]


def test_brief_flags_model_oi(client):
    d = client.get("/api/brief/NIFTY").json()
    pos_vote = next(v for v in d["votes"] if v["name"] == "positioning")
    if d["positioning"].get("model_oi"):
        assert "MODEL OI" in pos_vote["flag"]


def test_chain_carries_source_trail(client):
    d = client.get("/api/chain/NIFTY").json()
    assert "source_trail" in d
    # offline test mode must say so, not pretend
    assert any("offline" in t for t in d["source_trail"])
