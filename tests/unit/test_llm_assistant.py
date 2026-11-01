import httpx
import pytest

from codepilot.analysis import analyse_repository
from codepilot.assistant import CodeAssistant, build_proposal
from codepilot.config import Settings
from codepilot.errors import LLMUnavailableError, PatchRejectedError, SymbolNotFoundError
from codepilot.graphs import build_graphs
from codepilot.ingestion import scan
from codepilot.llm import AnthropicProvider, NullProvider, OpenAIProvider, make_provider


class FakeProvider:
    name = "fake"

    def __init__(self, reply: str):
        self.reply, self.prompts = reply, []

    def complete(self, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


def _assistant(repo, reply):
    model = analyse_repository(repo, scan(repo))
    return CodeAssistant(repo, model, build_graphs(model), FakeProvider(reply))


def test_explain_includes_grounded_context(small_repo):
    a = _assistant(small_repo, "Adds tax via `add_tax` [shop/pricing.py:7-8].")
    out = a.explain("shop.pricing:total")
    prompt = a.provider.prompts[0]
    assert "def total(items):" in prompt and "Called by: shop.cart:Cart.checkout" in prompt
    assert "Source of callee shop.tax:add_tax" in prompt
    assert out.references == ["shop/pricing.py:7-8"] and out.grounded


def test_hallucinated_citations_and_functions_are_flagged(small_repo):
    a = _assistant(small_repo, "It calls `apply_coupon()` [shop/pricing.py:500] and [shop/ghost.py:1].")
    out = a.explain("shop.pricing:total")
    assert not out.grounded
    assert out.invalid_citations == ["[shop/pricing.py:500]", "[shop/ghost.py:1]"]
    assert out.unknown_mentions == ["apply_coupon"]


def test_citation_outside_repo_is_invalid(small_repo):
    a = _assistant(small_repo, "see [../../etc/passwd.py:1]")
    assert a.explain("shop.pricing:total").invalid_citations


def test_unknown_symbol(small_repo):
    with pytest.raises(SymbolNotFoundError):
        _assistant(small_repo, "x").explain("shop.pricing:nope")


def test_patch_proposal_is_validated_not_applied(small_repo):
    a = _assistant(small_repo, "```python\ndef subtotal(items):\n    total = 0\n    for i in items:\n"
                               "        total += i['price'] * i['qty']\n    return total\n```")
    before = (small_repo / "shop/pricing.py").read_text()
    p = a.propose_patch("shop.pricing:subtotal", "make it a loop")
    assert (small_repo / "shop/pricing.py").read_text() == before
    assert p.diff.startswith("--- a/shop/pricing.py") and "+    for i in items:" in p.diff and not p.warnings


def test_patch_with_changed_signature_warns_and_bad_code_is_rejected(small_repo):
    model = analyse_repository(small_repo, scan(small_repo))
    fn = model.functions()["shop.pricing:subtotal"]
    assert "signature changed" in build_proposal(small_repo, fn, "def subtotal(items, x):\n    return 0\n").warnings[0]
    with pytest.raises(PatchRejectedError):
        build_proposal(small_repo, fn, "def subtotal(:\n")
    with pytest.raises(PatchRejectedError):
        build_proposal(small_repo, fn, "def other(items):\n    return 0\n")


def _transport(handler):
    return httpx.MockTransport(handler)


def test_openai_provider_parses_reply():
    def handler(req):
        assert req.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
    assert OpenAIProvider("k", "m", 5, _transport(handler)).complete("s", "p") == "hi"


def test_anthropic_provider_parses_reply():
    def handler(req):
        assert req.headers["x-api-key"] == "k"
        return httpx.Response(200, json={"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]})
    assert AnthropicProvider("k", "m", 5, _transport(handler)).complete("s", "p") == "ab"


def test_timeout_retries_then_fails():
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(LLMUnavailableError, match="timed out"):
        OpenAIProvider("k", "m", 0.1, _transport(handler), retries=1).complete("s", "p")
    assert len(calls) == 2


def test_rate_limit_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr("codepilot.llm.time.sleep", lambda s: None)
    replies = iter([httpx.Response(429), httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})])
    assert OpenAIProvider("k", "m", 5, _transport(lambda r: next(replies))).complete("s", "p") == "ok"


def test_http_error_and_bad_payload_raise_unavailable():
    with pytest.raises(LLMUnavailableError, match="HTTP 401"):
        OpenAIProvider("k", "m", 5, _transport(lambda r: httpx.Response(401))).complete("s", "p")
    with pytest.raises(LLMUnavailableError, match="request failed"):
        OpenAIProvider("k", "m", 5, _transport(lambda r: httpx.Response(200, json={}))).complete("s", "p")


def test_missing_key_and_null_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMUnavailableError):
        make_provider(Settings(llm_provider="openai"))
    assert isinstance(make_provider(Settings(llm_provider="none")), NullProvider)
    with pytest.raises(LLMUnavailableError):
        NullProvider().complete("s", "p")
