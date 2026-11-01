import pytest
from fastapi.testclient import TestClient

from codepilot.api.app import app, get_service
from codepilot.config import Settings
from codepilot.errors import LLMUnavailableError
from codepilot.service import CodePilotService

KEY = "test-key-123"
H = {"X-API-Key": KEY}


class FakeLLM:
    name = "fake"

    def __init__(self, reply="It totals the items [shop/pricing.py:7-8].", fail=False):
        self.reply, self.fail = reply, fail

    def complete(self, system, prompt):
        if self.fail:
            raise LLMUnavailableError("fake provider timed out")
        return self.reply


@pytest.fixture
def client(small_repo, tmp_path_factory):
    def make(provider=None, key=KEY):
        settings = Settings(api_key=key, allowed_roots=[small_repo.resolve()], test_timeout_s=60)
        svc = CodePilotService(settings, provider or FakeLLM(), tmp_path_factory.mktemp("state"))
        app.dependency_overrides[get_service] = lambda: svc
        return TestClient(app)
    yield make
    app.dependency_overrides.clear()


def test_health_needs_no_key(client):
    assert client().get("/health").json()["status"] == "ok"


def test_auth_is_required(client, small_repo):
    c = client()
    body = {"repo_path": str(small_repo)}
    assert c.post("/api/v1/analysis", json=body).status_code == 401
    assert c.post("/api/v1/analysis", json=body, headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.post("/api/v1/analysis", json=body, headers=H).status_code == 200


def test_server_without_key_refuses(client, small_repo):
    c = client(key=None)
    assert c.post("/api/v1/analysis", json={"repo_path": str(small_repo)}, headers=H).status_code == 503


def test_analysis_summary(client, small_repo):
    r = client().post("/api/v1/analysis", json={"repo_path": str(small_repo)}, headers=H).json()
    assert r["model"]["functions"] == 10 and r["module_cycles"] == [["shop.pricing", "shop.tax"]]
    assert r["recursive_functions"] == ["shop.cart:fact"]


def test_path_errors(client, small_repo, tmp_path_factory):
    c = client()
    outside = tmp_path_factory.mktemp("elsewhere")
    assert c.post("/api/v1/analysis", json={"repo_path": str(small_repo / "nope")}, headers=H).status_code == 404
    assert c.post("/api/v1/analysis", json={"repo_path": str(outside)}, headers=H).status_code == 403


def test_request_validation(client, small_repo):
    c = client()
    assert c.post("/api/v1/impact", json={"repo_path": str(small_repo), "qualname": "no-colon"},
                  headers=H).status_code == 422
    assert c.post("/api/v1/tests/run", json={"repo_path": str(small_repo), "test_paths": ["../etc"]},
                  headers=H).status_code == 422
    assert c.post("/api/v1/tests/run", json={"repo_path": str(small_repo), "test_paths": ["/abs"]},
                  headers=H).status_code == 422
    assert c.post("/api/v1/analysis", json={}, headers=H).status_code == 422


def test_impact_and_unknown_symbol(client, small_repo):
    c = client()
    ok = c.post("/api/v1/impact", json={"repo_path": str(small_repo), "qualname": "shop.tax:add_tax"}, headers=H)
    assert ok.status_code == 200 and ok.json()["affected_functions"]
    missing = c.post("/api/v1/impact", json={"repo_path": str(small_repo), "qualname": "shop.tax:nope"}, headers=H)
    assert missing.status_code == 404 and missing.json()["error"] == "SymbolNotFoundError"


def test_quality_filter(client, small_repo):
    r = client().post("/api/v1/quality", json={"repo_path": str(small_repo), "min_severity": "warning"},
                      headers=H).json()
    assert r["by_rule"] == {"import-cycle": 1}


def test_explain_and_llm_failure(client, small_repo):
    body = {"repo_path": str(small_repo), "qualname": "shop.pricing:total"}
    ok = client().post("/api/v1/explain", json=body, headers=H).json()
    assert ok["grounded"] and ok["valid_citations"] == ["[shop/pricing.py:7-8]"]
    down = client(FakeLLM(fail=True)).post("/api/v1/explain", json=body, headers=H)
    assert down.status_code == 503 and "timed out" in down.json()["detail"]


def test_generate_run_and_validate_tests(client, small_repo):
    c = client()
    gen = c.post("/api/v1/tests/generate", json={"repo_path": str(small_repo), "module": "shop.pricing"},
                 headers=H).json()
    assert gen["kept_tests"] > 0 and "from shop.pricing import" in gen["source"]
    # shop.tax cannot be imported first because of the fixture's circular import: reported, not crashed
    cyc = c.post("/api/v1/tests/generate", json={"repo_path": str(small_repo), "module": "shop.tax"}, headers=H).json()
    assert cyc["kept_tests"] == 0 and cyc["functions_skipped"]["add_tax"].startswith("module could not be imported")
    run = c.post("/api/v1/tests/run", json={"repo_path": str(small_repo)}, headers=H).json()
    assert run["passed"] == 1 and run["green"]
    val = c.post("/api/v1/tests/validate", headers=H, json={
        "repo_path": str(small_repo),
        "test_source": ("from shop.pricing import subtotal\n"
                        "def test_a():\n    assert subtotal([{'price': 2, 'qty': 3}]) == 6\n")}).json()
    assert val["summary"] == {"total": 1, "keep": 1}
    unknown = c.post("/api/v1/tests/generate", json={"repo_path": str(small_repo), "module": "shop.nope"}, headers=H)
    assert unknown.status_code == 404


def test_mutation_endpoint(client, small_repo):
    r = client().post("/api/v1/mutation", headers=H, json={
        "repo_path": str(small_repo), "source_files": ["shop/pricing.py"], "max_mutants": 5}).json()
    assert r["total"] == 3 and 0 <= r["score"] <= 100
    missing = client().post("/api/v1/mutation", headers=H, json={
        "repo_path": str(small_repo), "source_files": ["shop/none.py"]})
    assert missing.status_code == 422


def test_refactor_workflow_end_to_end(client, small_repo):
    c = client()
    new = ("def subtotal(items):\n    total = 0\n    for i in items:\n"
           "        total += i['price'] * i['qty']\n    return total\n")
    base = {"repo_path": str(small_repo)}
    both = c.post("/api/v1/refactor/propose", headers=H, json=base | {"qualname": "shop.pricing:subtotal"})
    assert both.status_code == 422
    rec = c.post("/api/v1/refactor/propose", headers=H,
                 json=base | {"qualname": "shop.pricing:subtotal", "new_code": new}).json()
    assert rec["status"] == "pending" and "total = 0" not in (small_repo / "shop/pricing.py").read_text()
    applied = c.post(f"/api/v1/refactor/{rec['id']}/approve", headers=H, json=base | {"approved_by": "me"}).json()
    assert applied["status"] == "applied" and "total = 0" in (small_repo / "shop/pricing.py").read_text()
    again = c.post(f"/api/v1/refactor/{rec['id']}/approve", headers=H, json=base | {"approved_by": "me"})
    assert again.status_code == 409
    rolled = c.post(f"/api/v1/refactor/{rec['id']}/rollback", headers=H, json=base).json()
    assert rolled["status"] == "rolled_back" and "total = 0" not in (small_repo / "shop/pricing.py").read_text()
    listed = c.get("/api/v1/refactor", params=base, headers=H).json()
    assert [p["status"] for p in listed] == ["rolled_back"]


def test_openapi_documents_all_routes(client):
    paths = client().get("/openapi.json").json()["paths"]
    assert {"/api/v1/analysis", "/api/v1/tests/generate", "/api/v1/refactor/{patch_id}/approve"} <= set(paths)
