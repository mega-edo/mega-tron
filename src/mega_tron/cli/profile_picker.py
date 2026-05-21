"""Embedder profile picker for `mega-tron setup`.

Maps three human-readable profile names to specific Hugging Face
embedder model ids. The mapping is anchored to measured F1 numbers
from the 200-query routing benchmark (pool=500) and to the per-model
CPU latency we observed on Apple Silicon — the picker is deliberately
opinionated so first-time users don't have to read the benchmark to
pick a sensible default.

Profile      → model id                                    F1 / latency
---------------------------------------------------------------------------
en-quality   → ThakiCloud/SKILLRET-Embedding-0.6B          0.892 / slow
en-fast      → BAAI/bge-small-en-v1.5                      0.884 / fast
multilingual → BAAI/bge-m3                                 0.840 / medium

`multilingual` is the install-wide DEFAULT_EMBEDDER_MODEL so users who
skip the picker (non-TTY install, --print-only, MEGA_QUIET, or
explicit `--profile auto`) land on the safest cross-language default.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    key: str
    model_id: str
    label: str
    blurb: str


# Order is the order shown to the user. `en-quality` is first because
# it is the measured-best F1 — users who don't know what they want
# pick the top option.
PROFILES: tuple[Profile, ...] = (
    Profile(
        key="en-quality",
        model_id="ThakiCloud/SKILLRET-Embedding-0.6B",
        label="English — quality (slower)",
        blurb=(
            "Best F1 on the routing benchmark (0.892). English-only. "
            "0.6B params — ~3-5× slower per query than the fast option. "
            "Pick this if all your prompts and skills are in English and "
            "you want maximum routing accuracy."
        ),
    ),
    Profile(
        key="en-fast",
        model_id="BAAI/bge-small-en-v1.5",
        label="English — fast",
        blurb=(
            "Near-best F1 (0.884) at a fraction of the cost. English-only. "
            "33M params — fastest of the three. Pick this for English-only "
            "workflows where hook latency matters."
        ),
    ),
    Profile(
        key="multilingual",
        model_id="BAAI/bge-m3",
        label="Multilingual (default)",
        blurb=(
            "F1 0.840 on the routing benchmark. 100+ languages "
            "(Korean / Japanese / Chinese / Arabic all first-class). "
            "568M params — medium speed. Pick this if you write prompts "
            "or have skill descriptions in any non-English language."
        ),
    ),
)


def profile_by_key(key: str) -> Profile | None:
    for p in PROFILES:
        if p.key == key:
            return p
    return None


def is_interactive() -> bool:
    """True only when we can safely prompt the user.

    Requires stdin AND stderr to be TTYs (we print the menu on stderr
    to keep stdout clean for ``--print-only`` consumers). Also honors
    ``MEGA_QUIET`` / ``CI`` / ``MEGA_TRON_NONINTERACTIVE`` opt-outs.
    """
    if os.environ.get("MEGA_QUIET"):
        return False
    if os.environ.get("CI"):
        return False
    if os.environ.get("MEGA_TRON_NONINTERACTIVE"):
        return False
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def prompt_for_profile() -> Profile | None:
    """Render the three-choice menu and return the user's pick.

    Returns ``None`` only when the read fails (EOF, KeyboardInterrupt)
    or the user enters something we don't recognise — the caller treats
    ``None`` as "leave config alone". There is deliberately no "skip"
    option: a fresh install already has the multilingual default
    saved, so "skip" would be indistinguishable from picking [3] and
    would just add noise to the menu. Users who want to change later
    can run ``mega-tron embedder set <id>``.
    """
    print(
        "\n"
        "[setup] mega-tron ships with three pre-tuned embedder profiles.\n"
        "        Pick one based on your prompt language and how much hook\n"
        "        latency you can tolerate (you can change later with\n"
        "        `mega-tron embedder set <huggingface-model-id>`).\n",
        file=sys.stderr,
    )
    for idx, p in enumerate(PROFILES, start=1):
        print(f"  [{idx}] {p.label}", file=sys.stderr)
        print(f"      {p.blurb}", file=sys.stderr)
        print(f"      model: {p.model_id}", file=sys.stderr)
        print("", file=sys.stderr)

    try:
        raw = input("Pick [1/2/3, default=3]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return None

    if raw == "":
        # Default — multilingual, matches the system-wide DEFAULT_EMBEDDER_MODEL.
        return profile_by_key("multilingual")
    if raw in {"1", "2", "3"}:
        return PROFILES[int(raw) - 1]
    # Allow the bare key too — handy for scripted re-runs.
    by_key = profile_by_key(raw)
    if by_key is not None:
        return by_key

    print(
        f"[setup] '{raw}' is not one of 1/2/3 — keeping current setting.",
        file=sys.stderr,
    )
    return None


def resolve_profile(
    cli_profile: str | None,
    *,
    config_is_default: bool,
) -> Profile | None:
    """Decide which profile (if any) to apply during ``mega-tron setup``.

    - ``cli_profile == "auto"`` and TTY available → prompt the user
    - ``cli_profile`` is a known key → return that profile directly
    - ``cli_profile == "auto"`` and no TTY → return ``None`` (defer to
      whatever is already saved in config, which is the multilingual
      default for fresh installs)
    - ``config_is_default`` is ``False`` (user already picked something
      via ``mega-tron embedder set``) → return ``None`` so we never
      stomp on their explicit choice during a re-run of setup

    Returns the chosen :class:`Profile`, or ``None`` if the caller
    should leave the saved embedder untouched.
    """
    if cli_profile and cli_profile != "auto":
        p = profile_by_key(cli_profile)
        if p is None:
            print(
                f"[setup] unknown --profile '{cli_profile}'. Valid: "
                f"{', '.join(p.key for p in PROFILES)}, auto. "
                "Keeping current embedder.",
                file=sys.stderr,
            )
            return None
        return p

    # cli_profile is None or "auto" from here on.
    if not config_is_default:
        # Respect prior `mega-tron embedder set` decisions on re-run.
        return None
    if not is_interactive():
        return None
    return prompt_for_profile()


__all__ = [
    "PROFILES",
    "Profile",
    "is_interactive",
    "profile_by_key",
    "prompt_for_profile",
    "resolve_profile",
]
