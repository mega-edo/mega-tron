"""Cross-host master skill pool.

The *master pool* is a single canonical directory holding skills that have
been explicitly *promoted* by the user. Each host (Codex, Claude Code,
Hermes, Gemini CLI) sees those skills via symlinks placed inside its own
skills directory, or (for Hermes specifically) via the
``skills.external_dirs`` config knob.

Layout::

    $XDG_DATA_HOME/mega-tron/pool/
        skills/
            <skill-name>/SKILL.md          # canonical content lives here
            <skill-name>/references/...
        manifest.json                       # per-skill provenance + mirror targets

Design rules:

1. **Existing host skills are never auto-moved.** The user runs
   ``mega-tron skills promote <name>`` explicitly. Promotion *moves*
   the skill directory into the pool and replaces the original with a
   symlink — the host keeps working unchanged, but the canonical copy is
   now central. ``unpromote`` reverses this.

2. **No silent overwrites.** If the user tries to promote a skill that
   already exists in the pool with different content (SHA mismatch), the
   operation fails unless ``--force`` is passed.

3. **Mirror symlinks are idempotent and reversible.** ``mirror --target
   <host>`` is safe to run repeatedly. ``unmirror`` only removes
   symlinks that point at the pool; it never touches independent files.

4. **The pool itself is invisible to discovery by default.** Cross-host
   visibility comes from the *mirrors* — that way each host's own
   discovery rules apply, and removing the mega-tron integration
   from a host doesn't leave broken references.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

from .config import data_dir

HostName = Literal["codex", "claude_code", "hermes", "gemini_cli"]
KNOWN_HOSTS: tuple[HostName, ...] = ("codex", "claude_code", "hermes", "gemini_cli")


def pool_root() -> Path:
    """``$MEGA_TRON_POOL`` or ``$XDG_DATA_HOME/mega-tron/pool``."""
    override = os.environ.get("MEGA_TRON_POOL", "").strip()
    if override:
        return Path(override).expanduser()
    return data_dir() / "pool"


def pool_skills_dir() -> Path:
    return pool_root() / "skills"


def pool_manifest_path() -> Path:
    return pool_root() / "manifest.json"


def host_skills_dir(host: HostName) -> Path:
    home = Path.home()
    if host == "codex":
        # CODEX_HOME wins when set, mirroring config.py's logic.
        codex_home = os.environ.get("CODEX_HOME", "").strip()
        return (Path(codex_home).expanduser() if codex_home else home / ".codex") / "skills"
    if host == "claude_code":
        return home / ".claude" / "skills"
    if host == "hermes":
        hermes_home = os.environ.get("HERMES_HOME", "").strip()
        return (Path(hermes_home).expanduser() if hermes_home else home / ".hermes") / "skills"
    if host == "gemini_cli":
        return home / ".gemini" / "skills"
    raise ValueError(f"unknown host: {host}")


# ---------------------------------------------------------------------------
# Manifest IO
# ---------------------------------------------------------------------------


@dataclass
class SkillRecord:
    """One row in the manifest. Tracks where a skill came from and where
    we've mirrored it. Stored verbatim in ``manifest.json``."""

    name: str
    promoted_from: str  # absolute path of the original host dir
    promoted_at: str  # ISO-8601 UTC
    content_sha: str
    mirrored_to: list[str] = field(default_factory=list)  # absolute paths


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_manifest() -> dict[str, SkillRecord]:
    path = pool_manifest_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, SkillRecord] = {}
    for name, row in (raw or {}).items():
        if not isinstance(row, dict):
            continue
        out[name] = SkillRecord(
            name=name,
            promoted_from=str(row.get("promoted_from", "")),
            promoted_at=str(row.get("promoted_at", "")),
            content_sha=str(row.get("content_sha", "")),
            mirrored_to=[str(p) for p in (row.get("mirrored_to") or [])],
        )
    return out


def _write_manifest(records: dict[str, SkillRecord]) -> None:
    path = pool_manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        r.name: {
            "promoted_from": r.promoted_from,
            "promoted_at": r.promoted_at,
            "content_sha": r.content_sha,
            "mirrored_to": sorted(set(r.mirrored_to)),
        }
        for r in records.values()
    }
    # atomic-ish: write to .tmp then replace
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------


def _dir_content_sha(skill_dir: Path) -> str:
    """Stable content hash over every regular file in ``skill_dir``.

    We hash ``relative_path + NUL + bytes`` in sorted order so unchanged
    skills produce identical SHAs across machines. Symlinks are followed
    (their target's contents are hashed) — promoting an already-symlinked
    skill is a degenerate case and the caller checks for that explicitly.
    """
    h = hashlib.sha256()
    files = []
    for p in skill_dir.rglob("*"):
        if p.is_file() or (p.is_symlink() and p.exists()):
            try:
                files.append((p.relative_to(skill_dir).as_posix(), p))
            except ValueError:
                continue
    files.sort(key=lambda x: x[0])
    for rel, fp in files:
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        try:
            h.update(fp.read_bytes())
        except OSError:
            continue
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


class PoolError(RuntimeError):
    """Operation refused (collision, missing target, etc)."""


@dataclass
class PromoteResult:
    name: str
    source: Path  # original host dir (now a symlink)
    target: Path  # new canonical pool location
    was_present: bool  # True if the pool already had this skill (SHA-match no-op)


def promote(
    name_or_path: str | Path,
    *,
    source_host: HostName | None = None,
    force: bool = False,
) -> PromoteResult:
    """Move a skill from its current host directory into the master pool.

    Resolution:
      - If ``name_or_path`` is a path, use it as-is.
      - Otherwise look it up under each known host's skills dir (only
        existing ones), respecting ``source_host`` if given. If the name
        is ambiguous across hosts, refuse.

    Behavior:
      - If the pool already has a skill of this name with the same
        SHA, no-op (record any missing mirror target the caller asks
        for separately).
      - If the pool has a different version, refuse unless ``force=True``
        (in which case we replace the pool copy and re-mirror).
      - The original host directory is replaced with a symlink to the
        pool copy. The host's own discovery sees it unchanged.
    """
    src = _resolve_source(name_or_path, source_host)
    name = src.name
    sha = _dir_content_sha(src)
    # Capture the source path BEFORE we touch the filesystem. Once
    # promote() replaces this path with a symlink, ``src.resolve()``
    # would silently follow the link into the pool directory — which
    # is exactly the opposite of what unpromote needs.
    original_host_path = str(src)

    pool_skills_dir().mkdir(parents=True, exist_ok=True)
    target = pool_skills_dir() / name
    records = _read_manifest()
    was_present = False

    if target.exists() and not target.is_symlink():
        existing_sha = _dir_content_sha(target)
        if existing_sha == sha:
            was_present = True
        elif not force:
            raise PoolError(
                f"pool already has {name!r} with different content "
                f"(pool sha={existing_sha[:12]}, source sha={sha[:12]}). "
                f"Pass force=True to overwrite."
            )
        else:
            shutil.rmtree(target)

    if not target.exists():
        # Move the source directory in. We move (not copy) so any
        # references that pointed at it via path now resolve to the
        # symlink we're about to plant.
        shutil.move(str(src), str(target))
    elif not was_present:
        # We get here only on force+overwrite path that already cleared
        # the target above; src is still in its original location.
        shutil.move(str(src), str(target))

    # Replace the original location with a symlink to the canonical copy.
    # If src vanished (because we moved it), recreate the symlink at the
    # same path so the host's discovery still finds it.
    if not src.exists():
        src.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, src)
    elif src.is_symlink():
        # idempotent: already a symlink. Repoint if needed.
        if Path(os.readlink(src)) != target:
            src.unlink()
            os.symlink(target, src)

    rec = records.get(name) or SkillRecord(
        name=name,
        promoted_from=original_host_path,
        promoted_at=_now_iso(),
        content_sha=sha,
    )
    rec.content_sha = sha
    if original_host_path not in rec.mirrored_to:
        rec.mirrored_to.append(original_host_path)
    records[name] = rec
    _write_manifest(records)

    return PromoteResult(name=name, source=src, target=target, was_present=was_present)


def unpromote(name: str) -> Path:
    """Reverse a ``promote``: move the canonical content back to the
    original host directory and remove all mirror symlinks.

    Returns the path it was restored to.
    """
    records = _read_manifest()
    rec = records.get(name)
    if rec is None:
        raise PoolError(f"skill {name!r} is not in the master pool")

    target = pool_skills_dir() / name
    if not target.exists():
        raise PoolError(f"pool dir for {name!r} missing on disk: {target}")

    # Remove every mirror symlink that still points at us — including
    # the one at the original host location (which is recorded as the
    # first entry in mirrored_to by promote()).
    for mp in list(rec.mirrored_to):
        p = Path(mp)
        if p.is_symlink() and Path(os.readlink(p)) == target:
            p.unlink()
        rec.mirrored_to.remove(mp)

    # Restore to the original host location. If, *after* we just cleared
    # symlinks above, that path is still occupied, it means the user
    # placed unrelated data there since promote() — refuse rather than
    # clobber.
    restore_to = Path(rec.promoted_from)
    if restore_to.exists():
        raise PoolError(
            f"original location {restore_to} is occupied; refusing to overwrite. "
            f"Remove it manually and rerun unpromote."
        )
    restore_to.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(restore_to))

    del records[name]
    _write_manifest(records)
    return restore_to


def mirror(target: HostName) -> list[Path]:
    """Ensure every pool skill has a symlink under the target host's
    skills directory. Returns the list of symlinks created or repointed.

    Existing files under the host dir with the same name are left
    untouched if they're not symlinks (user data takes precedence). If
    they *are* symlinks pointing somewhere else, we leave them alone
    and report nothing — the caller may want to know, but we don't
    silently repoint a user-managed link.
    """
    records = _read_manifest()
    dest_root = host_skills_dir(target)
    dest_root.mkdir(parents=True, exist_ok=True)
    changed: list[Path] = []
    for rec in records.values():
        canonical = pool_skills_dir() / rec.name
        dest = dest_root / rec.name
        if dest.exists() and not dest.is_symlink():
            continue  # respect user data
        if dest.is_symlink():
            if Path(os.readlink(dest)) == canonical:
                continue
            # Points elsewhere — leave alone, don't clobber.
            continue
        os.symlink(canonical, dest)
        changed.append(dest)
        if str(dest) not in rec.mirrored_to:
            rec.mirrored_to.append(str(dest))
    _write_manifest(records)
    return changed


def unmirror(target: HostName) -> list[Path]:
    """Remove every symlink under the target host's skills dir that
    points at the pool. Returns the list of removed symlinks. Non-symlink
    files are never touched."""
    records = _read_manifest()
    dest_root = host_skills_dir(target)
    if not dest_root.exists():
        return []
    canonical_root = pool_skills_dir().resolve(strict=False)
    removed: list[Path] = []
    for entry in dest_root.iterdir():
        if not entry.is_symlink():
            continue
        try:
            tgt = Path(os.readlink(entry))
        except OSError:
            continue
        tgt_resolved = tgt if tgt.is_absolute() else (entry.parent / tgt)
        try:
            tgt_resolved = tgt_resolved.resolve(strict=False)
        except OSError:
            continue
        if canonical_root in tgt_resolved.parents or tgt_resolved == canonical_root:
            entry.unlink()
            removed.append(entry)
    # Drop these from manifest mirror lists.
    for rec in records.values():
        rec.mirrored_to = [m for m in rec.mirrored_to if Path(m) not in removed]
    _write_manifest(records)
    return removed


def list_pool() -> list[SkillRecord]:
    """Return all pool records, sorted by name."""
    records = _read_manifest()
    return sorted(records.values(), key=lambda r: r.name)


def sync() -> dict[str, list[Path]]:
    """Recompute mirror state across all known hosts.

    Returns a per-host dict of newly-created symlinks. Hosts whose
    skills directory doesn't exist are silently skipped — the user
    must explicitly install mega-tron into that host first.
    """
    out: dict[str, list[Path]] = {}
    for host in KNOWN_HOSTS:
        if not host_skills_dir(host).exists():
            continue
        out[host] = mirror(host)
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_source(name_or_path: str | Path, source_host: HostName | None) -> Path:
    p = Path(name_or_path).expanduser()
    if p.is_absolute() or "/" in str(name_or_path):
        if not p.exists():
            raise PoolError(f"source path does not exist: {p}")
        if not (p / "SKILL.md").exists():
            raise PoolError(f"not a skill directory (no SKILL.md): {p}")
        return p

    name = str(name_or_path)
    candidates: list[Path] = []
    hosts: Iterable[HostName] = (
        (source_host,) if source_host else KNOWN_HOSTS
    )
    for host in hosts:
        d = host_skills_dir(host)
        if not d.exists():
            continue
        guess = d / name
        if (guess / "SKILL.md").exists():
            candidates.append(guess)
    if not candidates:
        raise PoolError(f"no host has a skill named {name!r}")
    if len(candidates) > 1:
        joined = ", ".join(str(c) for c in candidates)
        raise PoolError(
            f"skill {name!r} exists in multiple hosts: {joined}. "
            f"Pass source_host= to disambiguate."
        )
    return candidates[0]


__all__ = [
    "KNOWN_HOSTS",
    "HostName",
    "PoolError",
    "PromoteResult",
    "SkillRecord",
    "host_skills_dir",
    "list_pool",
    "mirror",
    "pool_manifest_path",
    "pool_root",
    "pool_skills_dir",
    "promote",
    "sync",
    "unmirror",
    "unpromote",
]
