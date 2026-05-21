"""LLM backend tests: codex (subprocess-mocked) + litellm (skip if missing)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from types import SimpleNamespace

import pytest

from mega_tron.llm_backends import LLMBackendError, make_llm_backend
from mega_tron.llm_backends.codex import CodexBackend, extract_message_text


def _fake_codex_stdout(text: str) -> str:
    """Build a minimal `codex exec --json` JSONL stream with the given text."""
    return "\n".join([
        json.dumps({"item": {"text": "hello?"}}),
        json.dumps({"item": {"text": text}}),
    ])


def test_codex_backend_invokes_with_model_flag(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return SimpleNamespace(
            returncode=0,
            stdout=_fake_codex_stdout("the answer is 42"),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("MEGA_QUIET", "1")
    out = CodexBackend(model="gpt-5.4-mini").chat("be brief", "what is 6 * 7?")
    assert out == "the answer is 42"
    # The invocation must carry the model flag and our read-only safety knobs.
    cmd = captured["cmd"]
    assert "codex" in cmd[0]
    assert cmd[1] == "exec"
    assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "gpt-5.4-mini"
    assert "--sandbox" in cmd and "read-only" in cmd
    assert "--skip-git-repo-check" in cmd
    assert "--json" in cmd


def test_codex_backend_nonzero_returncode_raises(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    monkeypatch.setenv("MEGA_QUIET", "1")
    with pytest.raises(LLMBackendError, match="rc=1"):
        CodexBackend().chat("s", "u")


def test_codex_backend_timeout_raises(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="codex", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setenv("MEGA_QUIET", "1")
    with pytest.raises(LLMBackendError, match="timed out"):
        CodexBackend().chat("s", "u", timeout_s=1)


def test_codex_backend_missing_binary_raises(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("codex")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setenv("MEGA_QUIET", "1")
    with pytest.raises(LLMBackendError, match="codex CLI not found"):
        CodexBackend().chat("s", "u")


def test_extract_message_text_uses_last_text_event():
    stdout = _fake_codex_stdout("FINAL")
    assert extract_message_text(stdout) == "FINAL"


def test_extract_message_text_strips_code_fences():
    stdout = json.dumps({"item": {"text": "```json\n{\"k\":1}\n```"}})
    assert extract_message_text(stdout) == '{"k":1}'


def test_extract_message_text_falls_back_to_plain_stdout():
    """If no JSONL parses, treat stdout as plain text."""
    assert extract_message_text("just a raw string") == "just a raw string"


# --- make_llm_backend() routing -------------------------------------------------


def test_make_llm_backend_defaults_to_codex(monkeypatch):
    monkeypatch.delenv("MEGA_BACKEND", raising=False)
    monkeypatch.delenv("MEGA_MODEL", raising=False)
    backend = make_llm_backend()
    assert backend.name == "codex"
    assert backend.model == "gpt-5.4-mini"


def test_make_llm_backend_codex_explicit_model(monkeypatch):
    monkeypatch.setenv("MEGA_BACKEND", "codex")
    monkeypatch.setenv("MEGA_MODEL", "gpt-5.4")
    backend = make_llm_backend()
    assert backend.name == "codex"
    assert backend.model == "gpt-5.4"


def test_make_llm_backend_unknown_raises(monkeypatch):
    monkeypatch.setenv("MEGA_BACKEND", "telepathy")
    with pytest.raises(LLMBackendError, match="unknown"):
        make_llm_backend()


# --- litellm path (skipped if package missing) ---------------------------------


HAS_LITELLM = importlib.util.find_spec("litellm") is not None


@pytest.mark.skipif(not HAS_LITELLM, reason="litellm not installed")
def test_litellm_backend_wires_completion(monkeypatch):
    import litellm  # type: ignore[import-not-found]

    from mega_tron.llm_backends.litellm_backend import LiteLLMBackend

    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "  hello back  "}}]}

    monkeypatch.setattr(litellm, "completion", fake_completion)
    monkeypatch.setenv("MEGA_QUIET", "1")

    backend = LiteLLMBackend(model="gpt-4o-mini")
    out = backend.chat("sys", "user", timeout_s=15)
    assert out == "hello back"
    assert captured["model"] == "gpt-4o-mini"
    assert captured["timeout"] == 15
    assert {"role": "system", "content": "sys"} in captured["messages"]
    assert {"role": "user", "content": "user"} in captured["messages"]


def test_litellm_backend_clear_error_when_missing(monkeypatch):
    """If litellm isn't importable, instantiation must point at the extra."""
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "litellm", None)  # force ImportError
    from mega_tron.llm_backends.litellm_backend import LiteLLMBackend

    with pytest.raises(LLMBackendError, match="agentic-litellm"):
        LiteLLMBackend()
