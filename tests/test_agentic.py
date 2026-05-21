"""Agentic search pipeline (v0.5) — stub backend, no real LLM."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import numpy as np
import pytest

from mega_tron.agentic import (
    PICK_SENTINEL_END,
    PICK_SENTINEL_START,
    AgenticResult,
    AgenticSearch,
    render_metadata_line,
)
from mega_tron.cache import Cache
from mega_tron.llm_backends import LLMBackendError
from mega_tron.router import Router


class _StubBackend:
    """Replays a scripted list of responses; raises if asked too many times."""

    name = "stub"

    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def chat(self, system: str, user: str, *, timeout_s: int = 30) -> str:
        self.calls.append((system, user))
        if not self.responses:
            raise AssertionError("stub backend ran out of scripted responses")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _wrap(payload: dict) -> str:
    """Wrap a dict payload between the canonical PICK sentinels."""
    return f"prelude\n{PICK_SENTINEL_START}\n{json.dumps(payload)}\n{PICK_SENTINEL_END}\nthe end"


def _build_router(skills_dir: Path, cache_path: Path, fake_embedder) -> Router:
    cache = Cache(path=cache_path)
    router = Router(skills_dir=skills_dir, embedder=fake_embedder, cache=cache)
    router.warmup()
    return router


@pytest.fixture
def mini_skills(tmp_path):
    """Three skills, distinct semantic neighborhoods for the FakeEmbedder."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "webhook-signer").mkdir()
    (skills / "webhook-signer" / "SKILL.md").write_text(
        '---\n'
        'name: webhook-signer\n'
        'description: "USE WHEN: validate HMAC webhook signature"\n'
        '---\n'
        '# webhook-signer\n\nrunbook body for webhook signing\n'
    )
    (skills / "git-amend-staged").mkdir()
    (skills / "git-amend-staged" / "SKILL.md").write_text(
        '---\nname: git-amend-staged\ndescription: "USE WHEN: amend staged git commit"\n---\n# body\n'
    )
    (skills / "url-safe-parser").mkdir()
    (skills / "url-safe-parser" / "SKILL.md").write_text(
        '---\nname: url-safe-parser\ndescription: "USE WHEN: parse user-supplied URLs safely"\n---\n# body\n'
    )
    return skills


def test_render_metadata_line_shape(fake_embedder, tmp_path, mini_skills):
    cache = Cache(path=tmp_path / "c.npz")
    router = Router(skills_dir=mini_skills, embedder=fake_embedder, cache=cache)
    router.warmup()
    entry = next(e for e in cache.entries() if e.name == "webhook-signer")
    line = render_metadata_line(entry)
    assert line.startswith("- webhook-signer:")
    assert "(h=0 ha=0 active)" in line
    assert "validate HMAC webhook signature" in line


def test_pick_single_call_no_reads(fake_embedder, tmp_path, mini_skills):
    """Step B alone is enough — backend returns a non-empty pick list."""
    cache = Cache(path=tmp_path / "c.npz")
    router = Router(skills_dir=mini_skills, embedder=fake_embedder, cache=cache)
    router.warmup()

    backend = _StubBackend([
        _wrap({"pick": ["webhook-signer"], "need_to_read": []})
    ])
    agentic = AgenticSearch(backend=backend, top=10, max_reads=3)
    q_vec = fake_embedder.embed(["validate hmac webhook signature"])[0]
    candidates = agentic.prefilter(cache, q_vec)
    assert "webhook-signer" in {e.name for e in candidates}

    result = agentic.pick("validate hmac webhook signature", candidates)
    assert result.picks == ["webhook-signer"]
    assert result.calls == 1
    assert result.reads == []
    assert not result.fell_back


def test_pick_step_c_disambiguates(fake_embedder, tmp_path, mini_skills):
    """need_to_read triggers a second LLM call with skill bodies."""
    cache = Cache(path=tmp_path / "c.npz")
    router = Router(skills_dir=mini_skills, embedder=fake_embedder, cache=cache)
    router.warmup()

    backend = _StubBackend([
        _wrap({"pick": [], "need_to_read": ["webhook-signer"]}),
        _wrap({"pick": ["webhook-signer", "url-safe-parser"]}),
    ])
    agentic = AgenticSearch(backend=backend, top=10, max_reads=3)
    q_vec = fake_embedder.embed(["webhook hmac"])[0]
    candidates = agentic.prefilter(cache, q_vec)
    result = agentic.pick("webhook hmac", candidates)

    assert result.picks == ["webhook-signer", "url-safe-parser"]
    assert result.calls == 2
    assert result.reads == ["webhook-signer"]
    # The system prompt of call #2 cited the body we loaded.
    assert "runbook body for webhook signing" in backend.calls[1][1]


def test_pick_step_c_max_reads_cap(fake_embedder, tmp_path, mini_skills):
    """max_reads bounds how many bodies the disambiguation call sees."""
    cache = Cache(path=tmp_path / "c.npz")
    router = Router(skills_dir=mini_skills, embedder=fake_embedder, cache=cache)
    router.warmup()

    backend = _StubBackend([
        _wrap({
            "pick": [],
            "need_to_read": ["webhook-signer", "git-amend-staged", "url-safe-parser"],
        }),
        _wrap({"pick": ["webhook-signer"]}),
    ])
    agentic = AgenticSearch(backend=backend, top=10, max_reads=1)
    q_vec = fake_embedder.embed(["webhook"])[0]
    candidates = agentic.prefilter(cache, q_vec)
    result = agentic.pick("webhook", candidates)

    assert result.calls == 2
    assert result.reads == ["webhook-signer"]  # capped at 1


def test_pick_fails_open_on_backend_error(fake_embedder, tmp_path, mini_skills):
    """Backend exceptions return AgenticResult(fell_back=True) and a sane top-N."""
    cache = Cache(path=tmp_path / "c.npz")
    router = Router(skills_dir=mini_skills, embedder=fake_embedder, cache=cache)
    router.warmup()

    backend = _StubBackend([LLMBackendError("boom")])
    agentic = AgenticSearch(backend=backend, top=10, max_reads=3)
    q_vec = fake_embedder.embed(["webhook"])[0]
    candidates = agentic.prefilter(cache, q_vec)
    result = agentic.pick("webhook", candidates)

    assert result.fell_back
    assert result.error and "boom" in result.error
    assert result.picks  # at least one cosine top-N name


def test_pick_step_b_shortlist_cap(fake_embedder, tmp_path, mini_skills):
    """v1.0: shortlist caps the LLM's `pick` list size from step B.

    A model that over-eagerly returns all 3 candidates is clipped to 2.
    """
    cache = Cache(path=tmp_path / "c.npz")
    router = Router(skills_dir=mini_skills, embedder=fake_embedder, cache=cache)
    router.warmup()

    backend = _StubBackend([
        _wrap({
            "pick": ["webhook-signer", "git-amend-staged", "url-safe-parser"],
            "need_to_read": [],
        }),
    ])
    agentic = AgenticSearch(backend=backend, top=10, shortlist=2, max_reads=3)
    q_vec = fake_embedder.embed(["webhook"])[0]
    candidates = agentic.prefilter(cache, q_vec)
    result = agentic.pick("webhook", candidates)

    assert len(result.picks) == 2
    # Order is the order the LLM emitted; shortlist clips from the tail.
    assert result.picks == ["webhook-signer", "git-amend-staged"]


def test_pick_shortlist_advertised_in_system_prompt(fake_embedder, tmp_path, mini_skills):
    """The step B system prompt cites the shortlist + max_reads + top_k budgets
    so the model knows the contract — v1.0 made these explicit knobs."""
    cache = Cache(path=tmp_path / "c.npz")
    router = Router(skills_dir=mini_skills, embedder=fake_embedder, cache=cache)
    router.warmup()

    backend = _StubBackend([
        _wrap({"pick": ["webhook-signer"], "need_to_read": []}),
    ])
    agentic = AgenticSearch(
        backend=backend, top=10, shortlist=7, top_k=3, max_reads=4,
    )
    q_vec = fake_embedder.embed(["webhook"])[0]
    candidates = agentic.prefilter(cache, q_vec)
    agentic.pick("webhook", candidates)

    system_prompt = backend.calls[0][0]
    # Each pipeline knob must surface in the prompt so the model
    # understands the budget — exact wording is implementation detail,
    # the numbers must show up.
    assert " 7 " in system_prompt or "7 skills" in system_prompt
    assert " 4 " in system_prompt or "4 of them" in system_prompt
    assert "top 3" in system_prompt


def test_pick_filters_unknown_names(fake_embedder, tmp_path, mini_skills):
    """If the LLM hallucinates a name not in candidates, we drop it."""
    cache = Cache(path=tmp_path / "c.npz")
    router = Router(skills_dir=mini_skills, embedder=fake_embedder, cache=cache)
    router.warmup()

    backend = _StubBackend([
        _wrap({"pick": ["webhook-signer", "ghost-skill"], "need_to_read": []})
    ])
    agentic = AgenticSearch(backend=backend, top=10, max_reads=3)
    q_vec = fake_embedder.embed(["webhook"])[0]
    candidates = agentic.prefilter(cache, q_vec)
    result = agentic.pick("webhook", candidates)
    assert result.picks == ["webhook-signer"]


def test_prefilter_drops_archived(fake_embedder, tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "active-one").mkdir()
    (skills / "active-one" / "SKILL.md").write_text(
        '---\nname: active-one\ndescription: "USE WHEN: webhook signing"\n---\n'
    )
    (skills / "dead-one").mkdir()
    (skills / "dead-one" / "SKILL.md").write_text(
        '---\nname: dead-one\ndescription: "USE WHEN: webhook signing"\n'
        'mega_meta:\n  status: archived\n---\n'
    )
    cache = Cache(path=tmp_path / "c.npz")
    router = Router(skills_dir=skills, embedder=fake_embedder, cache=cache)
    router.warmup()

    agentic = AgenticSearch(backend=_StubBackend([]), top=10)
    q_vec = fake_embedder.embed(["webhook"])[0]
    candidates = agentic.prefilter(cache, q_vec)
    names = {e.name for e in candidates}
    assert "active-one" in names
    assert "dead-one" not in names


def test_pick_empty_candidates_returns_empty(fake_embedder, tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    cache = Cache(path=tmp_path / "c.npz")
    Router(skills_dir=skills, embedder=fake_embedder, cache=cache).warmup()
    agentic = AgenticSearch(backend=_StubBackend([]), top=10)
    q_vec = fake_embedder.embed(["x"])[0]
    candidates = agentic.prefilter(cache, q_vec)
    result = agentic.pick("x", candidates)
    assert result == AgenticResult(picks=[], calls=0, reads=[], fell_back=False)


def test_pick_handles_code_fence_around_json(fake_embedder, tmp_path, mini_skills):
    """Model wraps the sentinel payload in a ```json fence — we still parse."""
    cache = Cache(path=tmp_path / "c.npz")
    router = Router(skills_dir=mini_skills, embedder=fake_embedder, cache=cache)
    router.warmup()

    backend = _StubBackend([
        f"{PICK_SENTINEL_START}\n```json\n"
        + json.dumps({"pick": ["webhook-signer"], "need_to_read": []})
        + f"\n```\n{PICK_SENTINEL_END}"
    ])
    agentic = AgenticSearch(backend=backend, top=10, max_reads=3)
    q_vec = fake_embedder.embed(["webhook hmac"])[0]
    candidates = agentic.prefilter(cache, q_vec)
    result = agentic.pick("webhook hmac", candidates)
    assert result.picks == ["webhook-signer"]


def test_router_rank_with_agentic_returns_picks_only(fake_embedder, tmp_path, mini_skills):
    """End-to-end: Router.rank routes through AgenticSearch when ``agentic`` is set."""
    cache = Cache(path=tmp_path / "c.npz")
    router = Router(skills_dir=mini_skills, embedder=fake_embedder, cache=cache)

    backend = _StubBackend([
        _wrap({"pick": ["git-amend-staged"], "need_to_read": []})
    ])
    # Disable the cosine-confidence short-circuit so we deterministically
    # exercise the LLM-pick path under stub conditions.
    agentic = AgenticSearch(
        backend=backend,
        top=10,
        max_reads=3,
        skip_when_confident=False,
    )
    ranked = router.rank("amend staged commit", top_k=3, agentic=agentic)
    names = [r.skill.name for r in ranked]
    assert names == ["git-amend-staged"]


def test_pick_custom_skill_body_loader(fake_embedder, tmp_path, mini_skills):
    """The body loader is injectable so tests / daemon can avoid filesystem hits."""
    cache = Cache(path=tmp_path / "c.npz")
    router = Router(skills_dir=mini_skills, embedder=fake_embedder, cache=cache)
    router.warmup()

    loaded: list[str] = []

    def fake_loader(entry):
        loaded.append(entry.name)
        return "FAKE BODY for " + entry.name

    backend = _StubBackend([
        _wrap({"pick": [], "need_to_read": ["webhook-signer"]}),
        _wrap({"pick": ["webhook-signer"]}),
    ])
    agentic = AgenticSearch(backend=backend, top=10, max_reads=3)
    q_vec = fake_embedder.embed(["webhook"])[0]
    candidates = agentic.prefilter(cache, q_vec)
    result = agentic.pick("webhook", candidates, skill_body_loader=fake_loader)
    assert result.reads == ["webhook-signer"]
    assert loaded == ["webhook-signer"]
    assert "FAKE BODY for webhook-signer" in backend.calls[1][1]
