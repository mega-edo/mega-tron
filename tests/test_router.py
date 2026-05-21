"""Router ranking + load_skills."""
from __future__ import annotations

from unittest.mock import MagicMock

from mega_tron.cache import Cache
from mega_tron.router import Router, Skill, load_skills


def test_load_skills_filters_invalid(fixtures_dir, tmp_path):
    """50 hand+programmatic fixtures must all pass validation."""
    invalid: list = []
    skills = load_skills(fixtures_dir / "skills", invalid=invalid)
    assert len(skills) == 50
    assert invalid == []


def test_load_skills_skips_non_skill_files(tmp_path):
    # Plain file in skills dir — should be ignored, not crash.
    (tmp_path / "README.md").write_text("not a skill")
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "SKILL.md").write_text(
        """---
name: x
description: a real skill
---
"""
    )
    skills = load_skills(tmp_path)
    assert [s.name for s in skills] == ["x"]


def test_router_rank_top_k(fake_embedder, fixtures_dir, tmp_cache_path):
    router = Router(
        skills_dir=fixtures_dir / "skills",
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    ranked = router.rank("webhook signature with hmac", top_k=5)
    assert len(ranked) == 5
    # Strictly descending scores.
    scores = [r.score for r in ranked]
    assert scores == sorted(scores, reverse=True)


def test_router_rank_prefilter_caps_blend_candidates(
    fake_embedder, fixtures_dir, tmp_cache_path
):
    """v1.0: `prefilter` clips the cosine candidates the eval-blend sees.

    With 50 fixture skills and prefilter=3, the result set must come from
    the cosine top-3 — anything ranked below that cosine cut cannot appear,
    no matter how strong its mega_meta signal would be.
    """
    router = Router(
        skills_dir=fixtures_dir / "skills",
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    full_ranked = router.rank("validate webhook hmac", top_k=50)
    cosine_top3_names = {r.skill.name for r in full_ranked[:3]}
    prefilter_ranked = router.rank(
        "validate webhook hmac", top_k=10, prefilter=3
    )
    # Every result with prefilter=3 must come from the cosine top-3.
    assert {r.skill.name for r in prefilter_ranked}.issubset(cosine_top3_names)
    # And at most 3 results since prefilter caps the candidate pool.
    assert len(prefilter_ranked) <= 3


def test_router_rank_prefilter_none_matches_full_blend(
    fake_embedder, fixtures_dir, tmp_cache_path
):
    """prefilter=None preserves v0.5 behaviour (blend across all entries)."""
    router = Router(
        skills_dir=fixtures_dir / "skills",
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    a = router.rank("webhook hmac", top_k=5)
    b = router.rank("webhook hmac", top_k=5, prefilter=None)
    assert [r.skill.name for r in a] == [r.skill.name for r in b]


def test_router_rank_webhook_query_surfaces_webhook_skill(fake_embedder, fixtures_dir, tmp_cache_path):
    router = Router(
        skills_dir=fixtures_dir / "skills",
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    ranked = router.rank("validate incoming webhook with hmac signature", top_k=10)
    names = [r.skill.name for r in ranked]
    # Hand-crafted webhook-signer should be in the top results because the
    # fake embedder weights `webhook`, `hmac`, `signature` keywords.
    assert "webhook-signer" in names


def test_router_rank_empty_skills_dir(fake_embedder, tmp_path, tmp_cache_path):
    router = Router(
        skills_dir=tmp_path,  # empty
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    ranked = router.rank("anything", top_k=5)
    assert ranked == []


def test_router_score_single_skill(fake_embedder, fixtures_dir, tmp_cache_path):
    """The single-skill score() entry point used by mega-symphony's hybrid hook."""
    router = Router(
        skills_dir=fixtures_dir / "skills",
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    router.warmup()  # populate cache
    skills = load_skills(fixtures_dir / "skills")
    webhook_skill = next(s for s in skills if s.name == "webhook-signer")
    git_skill = next(s for s in skills if s.name == "git-amend-staged")
    # Webhook query → webhook skill scores higher than git skill.
    webhook_q = "validate webhook hmac signature"
    s_web = router.score(webhook_q, webhook_skill)
    s_git = router.score(webhook_q, git_skill)
    assert s_web > s_git


def test_router_warmup_idempotent(fake_embedder, fixtures_dir, tmp_cache_path):
    router = Router(
        skills_dir=fixtures_dir / "skills",
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    n1, r1, _ = router.warmup()
    n2, r2, _ = router.warmup()
    assert n1 == 50 and r1 == 0
    assert n2 == 0 and r2 == 50


def test_router_score_caches_uncached_skill(fake_embedder, tmp_path, tmp_cache_path):
    """score() must persist its on-the-fly embedding so the next call doesn't re-embed.

    Regression for v0.1.0 behavior where _entry_for embedded but never inserted
    into the cache, costing one BGE forward pass per score() call on miss.
    """
    spy_embedder = MagicMock(wraps=fake_embedder)
    spy_embedder.model_id = fake_embedder.model_id
    spy_embedder.dim = fake_embedder.dim
    router = Router(
        skills_dir=tmp_path,  # empty — warmup loads nothing
        embedder=spy_embedder,
        cache=Cache(path=tmp_cache_path),
    )
    router.warmup()
    pre_calls = spy_embedder.embed.call_count

    cold = Skill(
        name="cold-skill",
        skill_dir=tmp_path,
        description="USE WHEN: cold",
        desc_tok=4,
        sha="sha-cold",
    )
    router.score("any query", cold)
    after_first = spy_embedder.embed.call_count
    router.score("another query", cold)
    after_second = spy_embedder.embed.call_count

    # First score(): one embed for query + one for the cold skill.
    # Second score(): only the query embeds; skill comes from cache.
    assert after_first - pre_calls == 2
    assert after_second - after_first == 1


def test_router_load_skills_dedup_on_duplicate_name(tmp_path):
    """Two SKILL.md files declaring the same `name:` should yield one Skill + one error."""
    for i in (1, 2):
        d = tmp_path / f"dir{i}"
        d.mkdir()
        (d / "SKILL.md").write_text(
            '---\nname: duped\ndescription: "USE WHEN: x"\n---\n'
        )
    invalid: list = []
    skills = load_skills(tmp_path, invalid=invalid)
    assert [s.name for s in skills] == ["duped"]
    assert len(invalid) == 1
    assert "duplicate skill name" in invalid[0].reason


def test_router_load_skills_name_collision_winner_by_verdict_score(tmp_path):
    """Two same-named skills, different verdict history → the one with
    the higher net score (helpful − harmful) wins, regardless of
    directory iteration order. This is the load_skills counterpart to
    Cache.compact_skills — both must agree on the winner-priority key.
    """
    # dir_a has the loser (cold start). dir_b has the winner (10 HELPFUL).
    # Alphabetic iteration would pick dir_a → assert that verdict signal
    # overrides that default.
    da = tmp_path / "dir_a"
    db = tmp_path / "dir_b"
    da.mkdir()
    db.mkdir()
    (da / "SKILL.md").write_text(
        '---\nname: tdd\ndescription: "USE WHEN: x"\n---\n'
    )
    (db / "SKILL.md").write_text(
        "---\n"
        "name: tdd\n"
        'description: "USE WHEN: x"\n'
        "mega_meta:\n"
        "  helpful_count: 10\n"
        "  harmful_count: 0\n"
        "---\n"
    )
    invalid: list = []
    skills = load_skills(tmp_path, invalid=invalid)
    assert [s.name for s in skills] == ["tdd"]
    # Winner is the dir_b copy.
    assert skills[0].helpful_count == 10
    # Loser is reported with the winner's path cited in the reason.
    assert len(invalid) == 1
    assert "duplicate skill name" in invalid[0].reason
    assert str(db / "SKILL.md") in invalid[0].reason


def test_router_load_skills_name_collision_winner_by_status(tmp_path):
    """Status (active > suspect > archived) outranks verdict score in
    the priority key — an archived sibling must never displace an
    active one even with a higher raw count.
    """
    da = tmp_path / "dir_a"
    db = tmp_path / "dir_b"
    da.mkdir()
    db.mkdir()
    # dir_a: active, modest verdict score.
    (da / "SKILL.md").write_text(
        "---\n"
        "name: tdd\n"
        'description: "USE WHEN: x"\n'
        "mega_meta:\n"
        "  helpful_count: 1\n"
        "  harmful_count: 0\n"
        "  status: active\n"
        "---\n"
    )
    # dir_b: archived, but huge raw score.
    (db / "SKILL.md").write_text(
        "---\n"
        "name: tdd\n"
        'description: "USE WHEN: x"\n'
        "mega_meta:\n"
        "  helpful_count: 100\n"
        "  harmful_count: 0\n"
        "  status: archived\n"
        "---\n"
    )
    skills = load_skills(tmp_path)
    assert skills[0].status == "active"
    assert skills[0].helpful_count == 1


def test_router_rank_evaluation_blend_reorders_same_cosine_skills(
    fake_embedder, tmp_path, tmp_cache_path
):
    """Two skills with identical semantic match — one with strong helpful_contexts
    matching the query, the other cold — should swap order under eval blend."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    # Cold skill — same description as the evaluated one (identical semantic).
    (skills_dir / "cold-webhook").mkdir()
    (skills_dir / "cold-webhook" / "SKILL.md").write_text(
        '---\nname: cold-webhook\ndescription: "USE WHEN: webhook hmac signature"\n---\n'
    )
    # Evaluated skill — same description + populated mega_meta whose
    # helpful_contexts mention webhook/hmac (will match our query embedding
    # because FakeEmbedder hits those keywords).
    (skills_dir / "evaluated-webhook").mkdir()
    (skills_dir / "evaluated-webhook" / "SKILL.md").write_text(
        '---\n'
        'name: evaluated-webhook\n'
        'description: "USE WHEN: webhook hmac signature"\n'
        'mega_meta:\n'
        '  helpful_count: 8\n'
        '  harmful_count: 0\n'
        '  helpful_contexts:\n'
        '    - "validated webhook hmac signature for incoming request"\n'
        '  harmful_contexts: []\n'
        '  status: active\n'
        '---\n'
    )

    router = Router(
        skills_dir=skills_dir,
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
        use_eval=True,
    )
    ranked = router.rank("webhook hmac signature", top_k=2)
    names = [r.skill.name for r in ranked]
    # Evaluated one wins because helpful_contexts match the query.
    assert names[0] == "evaluated-webhook"


def test_router_rank_no_eval_blend_returns_pure_semantic(
    fake_embedder, tmp_path, tmp_cache_path
):
    """use_eval=False reproduces v0.2 ordering — eval evidence ignored."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "archived-skill").mkdir()
    # An archived skill that would normally rank below everyone else.
    (skills_dir / "archived-skill" / "SKILL.md").write_text(
        '---\n'
        'name: archived-skill\n'
        'description: "USE WHEN: webhook hmac signature"\n'
        'mega_meta:\n'
        '  status: archived\n'
        '---\n'
    )
    (skills_dir / "active-skill").mkdir()
    (skills_dir / "active-skill" / "SKILL.md").write_text(
        '---\nname: active-skill\ndescription: "USE WHEN: webhook hmac signature"\n---\n'
    )
    router = Router(
        skills_dir=skills_dir,
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
        use_eval=False,   # explicit
    )
    ranked = router.rank("webhook hmac signature", top_k=2)
    names = [r.skill.name for r in ranked]
    # Both present — archived isn't suppressed when blend is off.
    assert set(names) == {"archived-skill", "active-skill"}


def test_router_rank_archived_excluded_under_eval_blend(
    fake_embedder, tmp_path, tmp_cache_path
):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "archived-skill").mkdir()
    (skills_dir / "archived-skill" / "SKILL.md").write_text(
        '---\n'
        'name: archived-skill\n'
        'description: "USE WHEN: webhook hmac signature"\n'
        'mega_meta:\n'
        '  status: archived\n'
        '---\n'
    )
    (skills_dir / "active-skill").mkdir()
    (skills_dir / "active-skill" / "SKILL.md").write_text(
        '---\nname: active-skill\ndescription: "USE WHEN: webhook hmac signature"\n---\n'
    )
    router = Router(
        skills_dir=skills_dir,
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
        use_eval=True,
    )
    ranked = router.rank("webhook hmac signature", top_k=2)
    assert ranked[0].skill.name == "active-skill"
    # archived-skill may still appear (top_k=2 returns 2) but its score is the
    # archived sentinel (-1.0), well below the active skill.
    assert ranked[0].score > ranked[-1].score
    assert ranked[-1].score == -1.0


# --------------------------------------------------------------------------- #
# Verdict-embedding signal integration (P2)
# --------------------------------------------------------------------------- #


def test_router_rank_related_verdict_helpful_boosts_skill(
    fake_embedder, tmp_path, tmp_cache_path, monkeypatch
):
    """A populated verdict-embeddings store with a similar HELPFUL
    verdict for skill A should rank A above an otherwise-tied skill B."""
    from mega_tron.verdicts.embeddings import VerdictEmbeddingsStore

    # Isolate the embedding store to this tmp dir.
    monkeypatch.setenv(
        "MEGA_TRON_VERDICT_EMBEDDINGS",
        str(tmp_path / "ve.npz"),
    )

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    for name in ("skill-a", "skill-b"):
        (skills_dir / name).mkdir()
        (skills_dir / name / "SKILL.md").write_text(
            f'---\nname: {name}\ndescription: "USE WHEN: webhook hmac signature"\n---\n'
        )

    # Seed the verdict store: skill-a has a HELPFUL verdict whose
    # embedded reason aligns with the query.
    ves = VerdictEmbeddingsStore(fingerprint=fake_embedder.fingerprint)
    helpful_emb = fake_embedder.embed(["webhook hmac signature verified"])[0]
    ves.append(
        verdict_id=1, skill_name="skill-a", label="HELPFUL",
        embedding=helpful_emb,
    )
    ves.save()

    router = Router(
        skills_dir=skills_dir,
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
        use_eval=True,
    )
    ranked = router.rank("webhook hmac signature", top_k=2)
    names = [r.skill.name for r in ranked]
    assert names[0] == "skill-a"
    # The boost is small but real — verify it's reflected in the score.
    score_a = next(r.score for r in ranked if r.skill.name == "skill-a")
    score_b = next(r.score for r in ranked if r.skill.name == "skill-b")
    assert score_a > score_b


def test_router_rank_related_verdict_harmful_penalises_skill(
    fake_embedder, tmp_path, tmp_cache_path, monkeypatch
):
    """A populated verdict-embeddings store with a similar HARMFUL
    verdict for skill A should rank A below an otherwise-tied skill B."""
    from mega_tron.verdicts.embeddings import VerdictEmbeddingsStore

    monkeypatch.setenv(
        "MEGA_TRON_VERDICT_EMBEDDINGS",
        str(tmp_path / "ve.npz"),
    )

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    for name in ("skill-a", "skill-b"):
        (skills_dir / name).mkdir()
        (skills_dir / name / "SKILL.md").write_text(
            f'---\nname: {name}\ndescription: "USE WHEN: webhook hmac signature"\n---\n'
        )

    ves = VerdictEmbeddingsStore(fingerprint=fake_embedder.fingerprint)
    harmful_emb = fake_embedder.embed(["broke webhook hmac signature handling"])[0]
    ves.append(
        verdict_id=1, skill_name="skill-a", label="HARMFUL",
        embedding=harmful_emb,
    )
    ves.save()

    router = Router(
        skills_dir=skills_dir,
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
        use_eval=True,
    )
    ranked = router.rank("webhook hmac signature", top_k=2)
    names = [r.skill.name for r in ranked]
    assert names[0] == "skill-b"


def test_router_rank_empty_verdict_store_changes_nothing(
    fake_embedder, tmp_path, tmp_cache_path, monkeypatch
):
    """An absent / empty verdict embeddings store means
    related_*_max=0 and the original three-layer ranking applies."""
    monkeypatch.setenv(
        "MEGA_TRON_VERDICT_EMBEDDINGS",
        str(tmp_path / "ve.npz"),
    )
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "skill-a").mkdir()
    (skills_dir / "skill-a" / "SKILL.md").write_text(
        '---\nname: skill-a\ndescription: "USE WHEN: webhook hmac signature"\n---\n'
    )
    router = Router(
        skills_dir=skills_dir,
        embedder=fake_embedder,
        cache=Cache(path=tmp_cache_path),
        use_eval=True,
    )
    ranked = router.rank("webhook hmac signature", top_k=1)
    assert ranked[0].skill.name == "skill-a"
    # Score should be the unblended cosine (no count_bonus, no contexts,
    # no related-verdict influence on a cold skill).
    assert ranked[0].score > 0
