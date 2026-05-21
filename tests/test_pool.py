"""Cross-host master skill pool — promote / unpromote / mirror semantics.

These tests pin behavior the user relies on once a skill is in the
shared pool:

- ``promote`` *moves* the source directory into the pool and replaces
  the original location with a symlink — both must be true.
- Two hosts can mirror the same canonical skill; each gets its own
  symlink pointing back at the pool.
- A second ``promote`` of the same skill is idempotent when the SHA
  matches, and *refuses* when it differs (unless force=True).
- ``unpromote`` reverses everything: skill returns to its original
  host directory, every mirror symlink is removed, manifest is empty.
- ``unmirror`` only deletes symlinks that point at *our* pool; user
  files and unrelated symlinks are untouched.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mega_tron import pool


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _seed_skill(host_skills_dir: Path, name: str, body: str = "stub body") -> Path:
    """Create a minimal valid skill under <host_skills_dir>/<name>/."""
    d = host_skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: A test skill named {name} for pool semantics.\n"
        "---\n"
        f"\n{body}\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def isolated_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``pool_root`` and every host skills dir into a tmp_path so
    the test never touches the real ``$HOME/.local/share/...`` or
    ``$HOME/.codex`` etc."""
    pool_dir = tmp_path / "pool"
    monkeypatch.setenv("MEGA_TRON_POOL", str(pool_dir))
    # Override host directories by patching home(). Cleanest hammer
    # available: rebind Path.home() inside the pool module's globals.
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    # Each known host now resolves to tmp_path/fake-home/.<host>/skills.
    (tmp_path / "fake-home" / ".codex" / "skills").mkdir(parents=True)
    (tmp_path / "fake-home" / ".claude" / "skills").mkdir(parents=True)
    (tmp_path / "fake-home" / ".hermes" / "skills").mkdir(parents=True)
    (tmp_path / "fake-home" / ".gemini" / "skills").mkdir(parents=True)
    # Defensive: clear any HERMES_HOME / CODEX_HOME so host_skills_dir
    # uses the patched HOME path resolution.
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return pool_dir


# --------------------------------------------------------------------------- #
# Promote
# --------------------------------------------------------------------------- #


def test_promote_moves_and_symlinks(isolated_pool: Path) -> None:
    codex_dir = pool.host_skills_dir("codex")
    src = _seed_skill(codex_dir, "webhook-signer", "v1 body")
    src_skill_md = src / "SKILL.md"
    expected_pool_path = pool.pool_skills_dir() / "webhook-signer"

    # Pre-conditions
    assert src.is_dir() and not src.is_symlink()
    assert not expected_pool_path.exists()

    result = pool.promote("webhook-signer")

    # The source path is now a symlink pointing at the pool location.
    assert src.is_symlink()
    assert Path(os.readlink(src)) == expected_pool_path

    # The canonical content moved into the pool.
    assert expected_pool_path.is_dir() and not expected_pool_path.is_symlink()
    assert (expected_pool_path / "SKILL.md").exists()
    assert (expected_pool_path / "SKILL.md").read_text(encoding="utf-8") == src_skill_md.read_text(encoding="utf-8")

    # The manifest carries one entry for this skill.
    records = pool.list_pool()
    assert len(records) == 1
    assert records[0].name == "webhook-signer"
    assert result.was_present is False


def test_promote_is_idempotent_on_sha_match(isolated_pool: Path) -> None:
    codex_dir = pool.host_skills_dir("codex")
    _seed_skill(codex_dir, "jwt-verifier")
    pool.promote("jwt-verifier")

    # A second promote of *the same* skill (now backed by a symlink) is
    # a no-op — pool already has it with identical SHA.
    r2 = pool.promote("jwt-verifier")
    assert r2.was_present is True
    assert len(pool.list_pool()) == 1


def test_promote_refuses_sha_mismatch_without_force(isolated_pool: Path) -> None:
    codex_dir = pool.host_skills_dir("codex")
    claude_dir = pool.host_skills_dir("claude_code")
    # Two hosts have *different* versions of the same-named skill.
    _seed_skill(codex_dir, "argon2-hash", "v1 body")
    pool.promote("argon2-hash", source_host="codex")

    _seed_skill(claude_dir, "argon2-hash", "v2 body — different content")
    with pytest.raises(pool.PoolError, match="different content"):
        pool.promote("argon2-hash", source_host="claude_code")

    # Force overwrites the pool copy.
    r = pool.promote("argon2-hash", source_host="claude_code", force=True)
    assert r.was_present is False
    body = (pool.pool_skills_dir() / "argon2-hash").rglob("*.md")
    assert any("v2 body" in p.read_text(encoding="utf-8") for p in body)


def test_promote_disambiguates_with_source_host(isolated_pool: Path) -> None:
    # Same skill name in two hosts — must require explicit source_host.
    _seed_skill(pool.host_skills_dir("codex"), "kafka-producer")
    _seed_skill(pool.host_skills_dir("claude_code"), "kafka-producer")

    with pytest.raises(pool.PoolError, match="multiple hosts"):
        pool.promote("kafka-producer")

    r = pool.promote("kafka-producer", source_host="codex")
    assert r.was_present is False


# --------------------------------------------------------------------------- #
# Mirror / unmirror
# --------------------------------------------------------------------------- #


def test_mirror_creates_symlinks_to_pool(isolated_pool: Path) -> None:
    _seed_skill(pool.host_skills_dir("codex"), "redis-ratelimit")
    pool.promote("redis-ratelimit")

    created = pool.mirror("hermes")
    expected_link = pool.host_skills_dir("hermes") / "redis-ratelimit"
    assert expected_link.is_symlink()
    assert expected_link in created
    assert Path(os.readlink(expected_link)) == pool.pool_skills_dir() / "redis-ratelimit"


def test_mirror_is_idempotent(isolated_pool: Path) -> None:
    _seed_skill(pool.host_skills_dir("codex"), "totp-mfa")
    pool.promote("totp-mfa")
    pool.mirror("hermes")
    # Second call: no new symlinks created.
    again = pool.mirror("hermes")
    assert again == []


def test_mirror_respects_user_data(isolated_pool: Path) -> None:
    # The host has a *user-managed* file with the same name as a pool
    # skill — mirror must NOT clobber it.
    _seed_skill(pool.host_skills_dir("codex"), "session-cookie")
    pool.promote("session-cookie")

    user_owned = pool.host_skills_dir("hermes") / "session-cookie"
    user_owned.mkdir()
    (user_owned / "SKILL.md").write_text("user-owned, do not touch", encoding="utf-8")

    pool.mirror("hermes")

    # Still a real directory, still has its original content.
    assert user_owned.is_dir() and not user_owned.is_symlink()
    assert "user-owned" in (user_owned / "SKILL.md").read_text(encoding="utf-8")


def test_unmirror_only_removes_pool_pointing_links(isolated_pool: Path) -> None:
    _seed_skill(pool.host_skills_dir("codex"), "csrf-token")
    pool.promote("csrf-token")
    pool.mirror("hermes")

    # Plant an *unrelated* symlink in the host dir — must survive.
    unrelated = pool.host_skills_dir("hermes") / "some-other-link"
    unrelated.symlink_to("/tmp/this-target-does-not-matter")

    removed = pool.unmirror("hermes")
    pool_link = pool.host_skills_dir("hermes") / "csrf-token"
    assert pool_link in removed
    assert not pool_link.exists()
    # The unrelated symlink is untouched.
    assert unrelated.is_symlink()


# --------------------------------------------------------------------------- #
# Unpromote
# --------------------------------------------------------------------------- #


def test_unpromote_round_trip(isolated_pool: Path) -> None:
    codex_dir = pool.host_skills_dir("codex")
    src = _seed_skill(codex_dir, "oauth-pkce", "round-trip body")
    original_md = (src / "SKILL.md").read_text(encoding="utf-8")
    pool.promote("oauth-pkce")
    pool.mirror("hermes")

    # Sanity: it's now a symlink + mirrored.
    assert src.is_symlink()
    assert (pool.host_skills_dir("hermes") / "oauth-pkce").is_symlink()

    # Unpromote: original-location symlink replaced with real dir;
    # mirror in hermes gone; pool empty.
    pool.unpromote("oauth-pkce")

    assert src.is_dir() and not src.is_symlink()
    assert (src / "SKILL.md").read_text(encoding="utf-8") == original_md
    assert not (pool.host_skills_dir("hermes") / "oauth-pkce").exists()
    assert (pool.pool_skills_dir() / "oauth-pkce").exists() is False
    assert pool.list_pool() == []


def test_unpromote_refuses_if_original_location_occupied(isolated_pool: Path) -> None:
    codex_dir = pool.host_skills_dir("codex")
    _seed_skill(codex_dir, "prisma-where")
    pool.promote("prisma-where")
    # Replace the symlink at the original location with an unrelated dir.
    link_path = codex_dir / "prisma-where"
    link_path.unlink()
    link_path.mkdir()
    (link_path / "USER_DATA.txt").write_text("don't clobber me", encoding="utf-8")

    with pytest.raises(pool.PoolError, match="occupied"):
        pool.unpromote("prisma-where")

    # User's data still there.
    assert (link_path / "USER_DATA.txt").exists()


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #


def test_sync_mirrors_to_every_existing_host(isolated_pool: Path) -> None:
    _seed_skill(pool.host_skills_dir("codex"), "k8s-rollout")
    pool.promote("k8s-rollout")

    result = pool.sync()
    # Every host's skills dir exists in the fixture → mirror succeeds
    # in each one.
    for host_name in ("codex", "claude_code", "hermes", "gemini_cli"):
        # The codex copy was the source; mirror created symlinks in the
        # OTHER hosts. For codex we already replaced the original with
        # a symlink at promote-time, so it's also a symlink now.
        link = pool.host_skills_dir(host_name) / "k8s-rollout"
        assert link.is_symlink(), f"{host_name} link missing"
