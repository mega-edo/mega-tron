"""Embedder profile picker — choice resolution, persistence, fallbacks.

`mega_tron.cli.profile_picker` is the small layer between the
`--profile {auto,en-quality,en-fast,multilingual}` flag (and its
interactive TTY prompt) and `Config.embedder_model`. The tests pin:

  - the three profile keys map to the exact HF model ids shipped today
  - explicit `--profile <key>` always wins, even when a non-TTY
    environment would otherwise refuse to prompt
  - `auto` defers to whatever is already saved when the user has
    customized their embedder (we never stomp an explicit choice)
  - `auto` without a TTY is a no-op (CI installs stay on the default)
  - the prompt parser accepts the documented inputs (1/2/3/s/blank)
"""
from __future__ import annotations

import pytest

from mega_tron.cli.profile_picker import (
    PROFILES,
    is_interactive,
    profile_by_key,
    prompt_for_profile,
    resolve_profile,
)


# ---- shape ----


def test_three_profiles_with_stable_keys_and_model_ids() -> None:
    """Profile mapping is the public contract — installs and tests
    depend on these exact ids. If you renumber or remap, update the
    setup docs and benchmarks in the same commit."""
    by_key = {p.key: p.model_id for p in PROFILES}
    assert by_key == {
        "en-quality": "ThakiCloud/SKILLRET-Embedding-0.6B",
        "en-fast": "BAAI/bge-small-en-v1.5",
        "multilingual": "BAAI/bge-m3",
    }


def test_profile_by_key_round_trip() -> None:
    for p in PROFILES:
        assert profile_by_key(p.key) is p
    assert profile_by_key("unknown") is None
    assert profile_by_key("") is None


# ---- resolve_profile (the policy layer) ----


def test_explicit_profile_wins_even_on_non_default_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI-supplied profile is the user's explicit instruction — we
    apply it whether or not they had previously customized. Without
    this, `mega-tron setup --profile en-fast` would silently no-op on
    any system that had already touched the embedder."""
    # Force "non-interactive" so we know the explicit branch is what
    # produced the answer, not a prompt.
    monkeypatch.setenv("MEGA_TRON_NONINTERACTIVE", "1")
    chosen = resolve_profile("en-fast", config_is_default=False)
    assert chosen is not None
    assert chosen.key == "en-fast"
    assert chosen.model_id == "BAAI/bge-small-en-v1.5"


def test_explicit_unknown_profile_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MEGA_TRON_NONINTERACTIVE", "1")
    chosen = resolve_profile("en-bogus", config_is_default=True)
    assert chosen is None
    err = capsys.readouterr().err
    assert "unknown --profile" in err


def test_auto_respects_existing_custom_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user already picked something via `mega-tron embedder
    set`, a setup re-run on `--profile auto` must not stomp it."""
    monkeypatch.setenv("MEGA_TRON_NONINTERACTIVE", "1")
    # Even with a TTY, config_is_default=False short-circuits the
    # picker so the saved choice survives. Force non-interactive too
    # for belt-and-braces.
    assert resolve_profile("auto", config_is_default=False) is None
    assert resolve_profile(None, config_is_default=False) is None


def test_auto_without_tty_is_noop_for_fresh_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI installs (no TTY) must not pause for input; they should land
    on the saved default, which `cmd_install` leaves untouched."""
    monkeypatch.setenv("MEGA_TRON_NONINTERACTIVE", "1")
    assert resolve_profile("auto", config_is_default=True) is None


def test_auto_with_tty_invokes_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When TTY-ish env vars allow it, `auto` delegates to
    `prompt_for_profile`. We stub the prompt to confirm the call
    happens without actually reading stdin."""
    monkeypatch.delenv("MEGA_QUIET", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("MEGA_TRON_NONINTERACTIVE", raising=False)
    monkeypatch.setattr(
        "mega_tron.cli.profile_picker.is_interactive", lambda: True
    )
    sentinel_profile = PROFILES[0]
    monkeypatch.setattr(
        "mega_tron.cli.profile_picker.prompt_for_profile",
        lambda: sentinel_profile,
    )
    assert resolve_profile("auto", config_is_default=True) is sentinel_profile


# ---- is_interactive opt-outs ----


@pytest.mark.parametrize(
    "env_var",
    ["MEGA_QUIET", "CI", "MEGA_TRON_NONINTERACTIVE"],
)
def test_is_interactive_respects_opt_out_env_vars(
    monkeypatch: pytest.MonkeyPatch, env_var: str
) -> None:
    """Three independent escape hatches: a user (MEGA_QUIET), a CI
    runner (CI), and the picker's own opt-out (MEGA_TRON_NONINTERACTIVE).
    Each one alone must be sufficient to silence the prompt."""
    for var in ("MEGA_QUIET", "CI", "MEGA_TRON_NONINTERACTIVE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(env_var, "1")
    assert is_interactive() is False


# ---- prompt_for_profile (input parsing) ----


def _patched_input(
    monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": response)


@pytest.mark.parametrize(
    "raw, expected_key",
    [
        ("1", "en-quality"),
        ("2", "en-fast"),
        ("3", "multilingual"),
        ("", "en-quality"),  # blank → default
        ("en-quality", "en-quality"),  # bare key works too
    ],
)
def test_prompt_recognizes_valid_inputs(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected_key: str
) -> None:
    _patched_input(monkeypatch, raw)
    chosen = prompt_for_profile()
    assert chosen is not None
    assert chosen.key == expected_key


def test_prompt_invalid_input_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patched_input(monkeypatch, "9")
    assert prompt_for_profile() is None
    assert "not one of" in capsys.readouterr().err


def test_prompt_eof_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_eof(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    assert prompt_for_profile() is None


def test_prompt_keyboard_interrupt_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_kbi(_prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", raise_kbi)
    assert prompt_for_profile() is None
