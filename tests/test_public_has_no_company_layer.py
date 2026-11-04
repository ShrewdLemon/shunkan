import pytest
from fastapi.testclient import TestClient

from shunkan.server.api import create_app

MOVED = ["/api/graph/health", "/api/msci/changes", "/api/funds", "/api/holder?name=x",
         "/api/entity/RELIANCE", "/api/company/RELIANCE", "/api/admin/llm", "/api/ownership/scan"]


@pytest.mark.parametrize("path", MOVED)
def test_company_layer_absent_without_desk(path):
    with TestClient(create_app(plugins=[])) as c:
        r = c.get(path)
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_core_routes_still_answer_alone():
    with TestClient(create_app(plugins=[])) as c:
        r = c.get("/api/participants")
        assert r.status_code in (200, 503) or "no participant data" in r.json()["detail"]
        assert c.get("/api/symbols/search?q=icici").status_code == 200


def test_core_status_has_no_graph_block():
    with TestClient(create_app(plugins=[])) as c:
        assert "graph" not in c.get("/api/status").json()
