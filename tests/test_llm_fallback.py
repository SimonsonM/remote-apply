"""Without Anthropic credit the drafting falls back to the local Ollama model instead of failing."""
import dspy
import smart


def test_fallback_switches_to_local(monkeypatch):
    calls = []
    def boom(self, *a, **k):
        raise RuntimeError("Your credit balance is too low to access the Anthropic API")
    monkeypatch.setattr(dspy.LM, "__call__", boom, raising=False)
    lm = smart._make_lm("anthropic/claude-sonnet-5-5", "ollama_chat/qwen2.5:14b")
    real_make = smart._make_lm
    def fake_make(model, local=None):
        calls.append(model)
        return (lambda *a, **k: ["local answer"]) if model.startswith("ollama") else real_make(model, local)
    monkeypatch.setattr(smart, "_make_lm", fake_make)
    assert lm(prompt="hi") == ["local answer"]
    assert calls == ["ollama_chat/qwen2.5:14b"]
