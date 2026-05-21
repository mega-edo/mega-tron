"""Hash-and-stamp the codex hook-trust state so `install` doesn't
require a manual `/hooks` trust step on first launch.

Hashes asserted here were captured from a live ``codex app-server
hooks/list`` invocation (codex 0.130.0) against a sandbox hooks.json.
Changing either the algorithm or the codex schema in a future release
will fail these tests loudly.
"""
from __future__ import annotations

from pathlib import Path

from mega_tron.hosts.codex.hook_trust import (
    DEFAULT_TIMEOUT_SEC,
    EVENT_LABEL,
    compute_hook_hash,
    compute_hook_key,
    render_trust_state_block,
)


def test_event_label_map_matches_codex_snake_case():
    # `hook_event_key_label` in codex-rs/hooks/src/lib.rs.
    assert EVENT_LABEL["UserPromptSubmit"] == "user_prompt_submit"
    assert EVENT_LABEL["Stop"] == "stop"
    assert EVENT_LABEL["PreToolUse"] == "pre_tool_use"


def test_compute_hook_hash_matches_codex_authoritative_output():
    """Authoritative golden values pulled from codex 0.130.0 via
    ``codex app-server hooks/list`` for a hooks.json that registered::

        UserPromptSubmit -> /tmp/msr-hash-test-19964/bin/msr-hook-spy hook
        Stop             -> /tmp/msr-hash-test-19964/bin/msr-hook-spy stop-hook
    """
    cmd_ups = "/tmp/msr-hash-test-19964/bin/msr-hook-spy hook"
    cmd_stop = "/tmp/msr-hash-test-19964/bin/msr-hook-spy stop-hook"
    assert (
        compute_hook_hash("user_prompt_submit", cmd_ups)
        == "sha256:5b561c3b273211572bdbe0f9824b4919576b83492f74bd2c79101e9164472a83"
    )
    assert (
        compute_hook_hash("stop", cmd_stop)
        == "sha256:c1ff762fa29d0de10beec22c4fcc71081b0a79e6c9dea06f2e18eadce246d16f"
    )


def test_compute_hook_hash_is_deterministic():
    h1 = compute_hook_hash("user_prompt_submit", "foo --bar")
    h2 = compute_hook_hash("user_prompt_submit", "foo --bar")
    assert h1 == h2


def test_compute_hook_hash_distinguishes_event_and_command():
    base = compute_hook_hash("user_prompt_submit", "mega-tron hook")
    different_event = compute_hook_hash("stop", "mega-tron hook")
    different_command = compute_hook_hash(
        "user_prompt_submit", "mega-tron hook --different"
    )
    assert base != different_event
    assert base != different_command


def test_compute_hook_key_uses_canonical_path(tmp_path):
    """Codex canonicalizes the hooks.json path (resolves symlinks) before
    using it in the trust-state key; we do the same with Path.resolve()."""
    real = tmp_path / "real.json"
    real.write_text("{}")
    symlink = tmp_path / "link.json"
    symlink.symlink_to(real)
    key_real = compute_hook_key(real, "user_prompt_submit")
    key_via_symlink = compute_hook_key(symlink, "user_prompt_submit")
    assert key_real == key_via_symlink
    assert key_real.endswith(":user_prompt_submit:0:0")


def test_compute_hook_key_includes_group_and_handler_indexes(tmp_path):
    p = tmp_path / "hooks.json"
    p.write_text("{}")
    k = compute_hook_key(p, "stop", group_index=2, handler_index=5)
    assert k.endswith(":stop:2:5")


def test_render_trust_state_block_only_stamps_managed_entries(tmp_path):
    """Render must skip any entry that doesn't carry the managed marker —
    we never silently trust a user-authored hook."""
    p = tmp_path / "hooks.json"
    p.write_text("{}")
    hooks_data = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "_mega_tron_managed": "0.5.0",
                    "matcher": ".*",
                    "hooks": [
                        {"type": "command", "command": "mega-tron hook"}
                    ],
                },
                {
                    # User's own hook — must NOT be trusted automatically.
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "user-cmd"}],
                },
            ]
        }
    }
    block = render_trust_state_block(
        p, hooks_data, only_managed_keys={"_mega_tron_managed"}
    )
    assert "mega-tron hook" not in block  # ← the command itself isn't in the block
    assert "trusted_hash" in block
    expected_hash = compute_hook_hash("user_prompt_submit", "mega-tron hook")
    assert expected_hash in block
    # User's bash hook is NOT trusted.
    user_hash = compute_hook_hash("user_prompt_submit", "user-cmd")
    assert user_hash not in block


def test_render_trust_state_block_empty_when_nothing_managed(tmp_path):
    p = tmp_path / "hooks.json"
    p.write_text("{}")
    block = render_trust_state_block(
        p,
        {"hooks": {"UserPromptSubmit": [{"matcher": "x", "hooks": []}]}},
        only_managed_keys={"_mega_tron_managed"},
    )
    assert block == ""


def test_render_trust_state_block_handles_both_events(tmp_path):
    p = tmp_path / "hooks.json"
    p.write_text("{}")
    hooks_data = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "_mega_tron_managed": "0.5.0",
                    "matcher": ".*",
                    "hooks": [
                        {"type": "command", "command": "mega-tron hook"}
                    ],
                }
            ],
            "Stop": [
                {
                    "_mega_tron_managed": "0.5.0",
                    "matcher": ".*",
                    "hooks": [
                        {"type": "command", "command": "mega-tron stop-hook"}
                    ],
                }
            ],
        }
    }
    block = render_trust_state_block(
        p, hooks_data, only_managed_keys={"_mega_tron_managed"}
    )
    canon = str(p.resolve())
    assert f'[hooks.state."{canon}:user_prompt_submit:0:0"]' in block
    assert f'[hooks.state."{canon}:stop:0:0"]' in block


def test_default_timeout_matches_codex():
    """Codex normalizes the timeout to 600s on hash; if a future codex
    release changes the default, this golden anchor will fail loudly."""
    assert DEFAULT_TIMEOUT_SEC == 600
