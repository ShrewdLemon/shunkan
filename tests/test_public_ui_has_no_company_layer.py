from pathlib import Path

APP = (Path(__file__).parent.parent / "src/shunkan/server/static/app.js").read_text()


def test_company_renderers_are_gone():
    for name in ("renderCompany", "renderFunds", "renderMSCI", "renderGraph", "renderNetwork", "renderAdmin"):
        assert f"function {name}(" not in APP, name


def test_no_calls_to_desk_routes():
    for route in ("/api/funds", "/api/graph", "/api/msci", "/api/entity", "/api/holder",
                  "/api/ownership", "/api/admin/llm", "/api/company/"):
        assert route not in APP, route
