"""End-to-end integration: 50 fixture skills → cache → rank → stage → prepend.

Uses the FakeEmbedder so it stays under 1s and CI-friendly.
"""
from __future__ import annotations

import json

from mega_tron.cache import Cache
from mega_tron.prepender import build_prefix
from mega_tron.router import Router
from mega_tron.stager import Stager


def test_end_to_end_standalone(fake_embedder, fixtures_dir, tmp_cache_path, tmp_codex_home):
    """Mirror examples/standalone_usage.py — full pipeline against 50 fixtures."""
    # 1. Router warms up — embeds all 50, caches.
    router = Router(
        skills_dir=fixtures_dir / "skills",
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    n_new, n_reused, invalid = router.warmup()
    assert n_new == 50
    assert n_reused == 0
    assert invalid == []

    # 2. Cache persisted; second warmup reuses everything.
    router2 = Router(
        skills_dir=fixtures_dir / "skills",
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    n_new2, n_reused2, _ = router2.warmup()
    assert n_new2 == 0
    assert n_reused2 == 50

    # 3. Rank for a representative ticket — webhook signing.
    ranked = router2.rank(
        "Implement HMAC-SHA256 webhook signature verification with replay protection",
        top_k=5,
    )
    assert len(ranked) == 5
    # webhook-signer is the hand-crafted relevant fixture.
    names = [r.skill.name for r in ranked]
    assert "webhook-signer" in names

    # 4. Stage top-K into codex_home.
    stager = Stager(target=tmp_codex_home, budget_tok=1500)
    manifest = stager.stage(ranked)
    assert len(manifest.staged) > 0
    assert manifest.total_staged_tok <= 1500

    # 5. Manifest written to JSON.
    manifest_file = tmp_codex_home / "codex_home_staged.json"
    data = json.loads(manifest_file.read_text())
    assert data["total_staged_tok"] == manifest.total_staged_tok

    # 6. Symlink farm populated.
    farm = tmp_codex_home / "skills"
    staged_names = {s.name for s in manifest.staged}
    farm_names = {p.name for p in farm.iterdir()}
    assert staged_names == farm_names

    # 7. Prepend prefix is a non-forcing candidate-skills string.
    prefix = build_prefix(ranked, k=3)
    assert prefix.startswith("Candidate skills for this task:")
    assert prefix.endswith("Use whichever fit; ignore the rest.\n\n")
    # No `$` token — codex's MUST-use rule must not be triggered.
    assert "$" not in prefix
    # Top-3 names appear (bare, no `$` prefix).
    for rs in ranked[:3]:
        assert rs.skill.name in prefix


def test_end_to_end_different_query_picks_different_skills(
    fake_embedder, fixtures_dir, tmp_cache_path, tmp_codex_home
):
    """A git-amend query and a webhook query must rank distinct skills first."""
    router = Router(
        skills_dir=fixtures_dir / "skills",
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    a = router.rank("amend my last git commit before pushing", top_k=5)
    b = router.rank("validate webhook hmac signature", top_k=5)
    a_top = a[0].skill.name
    b_top = b[0].skill.name
    assert a_top != b_top
