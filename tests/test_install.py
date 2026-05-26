"""Installer: rc-file mutation + wrapper rendering + shell-level integration."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import pytest

from mega_tron.hosts.codex.install import (
    CODEX_CONFIG_SENTINEL_END,
    CODEX_CONFIG_SENTINEL_START,
    CODEX_TRUST_SENTINEL_END,
    CODEX_TRUST_SENTINEL_START,
    HOOK_MANAGED_KEY,
    SENTINEL_END,
    SENTINEL_START,
    _hook_entry,
    _install_codex_config_toml,
    _install_codex_trust,
    _merge_hooks_json,
    _replace_block,
    _strip_block,
    _strip_managed_hooks,
    _uninstall_codex_config_toml,
    _user_has_unmanaged_skills_table,
    render_codex_config_block,
    render_codex_trust_block,
    render_snippet,
    run_install,
)


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(
        shell="zsh",
        rc_file=None,
        skills_dir=str(Path.home() / ".codex" / "skills"),
        codex_home=str(Path.home() / ".local" / "share" / "mega-tron" / "codex-home"),
        budget_tok=1500,
        top_k=5,
        uninstall=False,
        print_only=True,
        no_warmup=True,
        no_hook=False,
        codex_hooks_file=None,
        hook_command=None,
        # Default to skipping the codex config.toml patch in tests so we
        # never accidentally touch the real ~/.codex/config.toml. Tests
        # that exercise the patcher set keep_codex_catalog=False AND
        # codex_config_path=<tmp path>.
        keep_codex_catalog=True,
        codex_config_path=None,
        no_agents_md=True,
        agents_md_path=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_render_snippet_substitutes_defaults():
    out = render_snippet(
        skills_dir="/SK",
        codex_home="/CH",
        top_k=7,
        budget_tok=999,
    )
    assert SENTINEL_START in out
    assert SENTINEL_END in out
    assert "/SK" in out
    assert "/CH" in out
    assert "MEGA_TOP_K:-7" in out
    assert "MEGA_BUDGET_TOK:-999" in out


def test_replace_block_inserts_when_absent():
    base = "# user line\nexport FOO=1\n"
    new = render_snippet(
        skills_dir="/SK", codex_home="/CH", top_k=5, budget_tok=1500
    )
    out = _replace_block(base, new)
    assert base.rstrip() in out
    assert new in out


def test_replace_block_is_idempotent():
    """Two consecutive installs produce byte-identical output."""
    new = render_snippet(
        skills_dir="/SK", codex_home="/CH", top_k=5, budget_tok=1500
    )
    base = "# user\n"
    first = _replace_block(base, new)
    second = _replace_block(first, new)
    assert first == second


def test_strip_block_removes_only_managed_section():
    new = render_snippet(
        skills_dir="/SK", codex_home="/CH", top_k=5, budget_tok=1500
    )
    pre = "# user prelude\nexport FOO=1\n"
    post = "# user trailer\n"
    full = pre + new + post
    stripped = _strip_block(full)
    assert stripped == pre + post


def test_install_writes_rc_file(tmp_path):
    rc = tmp_path / "rc"
    run_install(
        _args(
            rc_file=str(rc),
            print_only=False,
            skills_dir=str(tmp_path / "nonexistent"),
        )
    )
    body = rc.read_text()
    assert "codex() {" in body
    assert SENTINEL_START in body
    assert SENTINEL_END in body


def test_install_print_only_does_not_touch_rc(tmp_path, capsys):
    rc = tmp_path / "rc"
    run_install(
        _args(
            rc_file=str(rc),
            print_only=True,
            skills_dir=str(tmp_path / "nonexistent"),
        )
    )
    assert not rc.exists()
    out = capsys.readouterr().out
    assert "codex() {" in out


# --------------------------------------------------------------------------
# Codex hooks.json merge
# --------------------------------------------------------------------------


def test_merge_hooks_json_inserts_into_empty():
    from mega_tron.hosts.codex.install import HOOK_MANAGED_VERSION

    out = _merge_hooks_json({}, _hook_entry("mega-tron hook"))
    ups = out["hooks"]["UserPromptSubmit"]
    assert len(ups) == 1
    assert ups[0][HOOK_MANAGED_KEY] == HOOK_MANAGED_VERSION
    assert ups[0]["hooks"][0]["command"] == "mega-tron hook"


def test_merge_hooks_json_preserves_user_entries():
    existing = {
        "hooks": {
            "UserPromptSubmit": [
                {"matcher": "user-existing", "hooks": [{"type": "command", "command": "user-cmd"}]}
            ],
            "PostToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "fmt"}]}
            ],
        }
    }
    out = _merge_hooks_json(existing, _hook_entry("mega-tron hook"))
    ups = out["hooks"]["UserPromptSubmit"]
    assert len(ups) == 2
    # User entry first (preserved order).
    assert ups[0]["matcher"] == "user-existing"
    # Managed entry appended.
    assert HOOK_MANAGED_KEY in ups[1]
    # Other event names untouched.
    assert out["hooks"]["PostToolUse"] == existing["hooks"]["PostToolUse"]


def test_merge_hooks_json_replaces_old_managed_entry():
    """Re-running install must update, not duplicate, the managed entry."""
    first = _merge_hooks_json({}, _hook_entry("old-cmd"))
    second = _merge_hooks_json(first, _hook_entry("new-cmd"))
    ups = second["hooks"]["UserPromptSubmit"]
    assert len(ups) == 1
    assert ups[0]["hooks"][0]["command"] == "new-cmd"


def test_strip_managed_hooks_leaves_user_entries():
    merged = _merge_hooks_json(
        {
            "hooks": {
                "UserPromptSubmit": [
                    {"matcher": "u", "hooks": [{"type": "command", "command": "user"}]}
                ]
            }
        },
        _hook_entry("mega-tron hook"),
    )
    stripped = _strip_managed_hooks(merged)
    ups = stripped["hooks"]["UserPromptSubmit"]
    assert len(ups) == 1
    assert ups[0]["matcher"] == "u"


def test_strip_managed_hooks_removes_empty_containers():
    """If our entry was the only one, the parent keys disappear."""
    merged = _merge_hooks_json({}, _hook_entry("mega-tron hook"))
    stripped = _strip_managed_hooks(merged)
    assert stripped == {}


def test_install_writes_hooks_json_and_preserves_user_entries(tmp_path):
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [
                {"matcher": "u", "hooks": [{"type": "command", "command": "user-cmd"}]}
            ]
        }
    }))
    rc_file = tmp_path / "rc"
    run_install(_args(
        rc_file=str(rc_file),
        codex_hooks_file=str(hooks_file),
        print_only=False,
        skills_dir=str(tmp_path / "nope"),
    ))
    data = json.loads(hooks_file.read_text())
    ups = data["hooks"]["UserPromptSubmit"]
    assert any(e.get("matcher") == "u" for e in ups)
    assert any(HOOK_MANAGED_KEY in e for e in ups)


def test_install_uninstall_removes_only_managed_hook(tmp_path):
    hooks_file = tmp_path / "hooks.json"
    rc_file = tmp_path / "rc"
    # First install
    run_install(_args(
        rc_file=str(rc_file),
        codex_hooks_file=str(hooks_file),
        print_only=False,
        skills_dir=str(tmp_path / "nope"),
    ))
    # Add an unrelated user hook AFTER install
    data = json.loads(hooks_file.read_text())
    data["hooks"]["UserPromptSubmit"].append(
        {"matcher": "user-after", "hooks": [{"type": "command", "command": "ucmd"}]}
    )
    hooks_file.write_text(json.dumps(data))
    # Uninstall
    run_install(_args(
        rc_file=str(rc_file),
        codex_hooks_file=str(hooks_file),
        uninstall=True,
    ))
    data = json.loads(hooks_file.read_text())
    ups = data["hooks"]["UserPromptSubmit"]
    assert len(ups) == 1
    assert ups[0]["matcher"] == "user-after"


def test_install_registers_both_user_prompt_and_stop_hooks(tmp_path):
    """The new dual-hook install must write entries for both managed events."""
    hooks_file = tmp_path / "hooks.json"
    rc_file = tmp_path / "rc"
    run_install(_args(
        rc_file=str(rc_file),
        codex_hooks_file=str(hooks_file),
        print_only=False,
        skills_dir=str(tmp_path / "nope"),
    ))
    data = json.loads(hooks_file.read_text())
    assert HOOK_MANAGED_KEY in data["hooks"]["UserPromptSubmit"][0]
    assert HOOK_MANAGED_KEY in data["hooks"]["Stop"][0]
    # Each entry uses the right subcommand.
    ups_cmd = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    stop_cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert ups_cmd.endswith(" hook")
    assert stop_cmd.endswith(" stop-hook")


def test_install_uninstall_strips_both_events(tmp_path):
    hooks_file = tmp_path / "hooks.json"
    rc_file = tmp_path / "rc"
    run_install(_args(
        rc_file=str(rc_file),
        codex_hooks_file=str(hooks_file),
        print_only=False,
        skills_dir=str(tmp_path / "nope"),
    ))
    # Add an unrelated user hook on BOTH events
    data = json.loads(hooks_file.read_text())
    data["hooks"]["UserPromptSubmit"].append(
        {"matcher": "u", "hooks": [{"type": "command", "command": "u"}]}
    )
    data["hooks"]["Stop"].append(
        {"matcher": "s", "hooks": [{"type": "command", "command": "s"}]}
    )
    hooks_file.write_text(json.dumps(data))
    # Uninstall
    run_install(_args(
        rc_file=str(rc_file),
        codex_hooks_file=str(hooks_file),
        uninstall=True,
    ))
    data = json.loads(hooks_file.read_text())
    # Both events retain only the user-owned entries.
    assert len(data["hooks"]["UserPromptSubmit"]) == 1
    assert data["hooks"]["UserPromptSubmit"][0]["matcher"] == "u"
    assert len(data["hooks"]["Stop"]) == 1
    assert data["hooks"]["Stop"][0]["matcher"] == "s"


# --------------------------------------------------------------------------
# Codex config.toml — skills.include_instructions = false patcher
# --------------------------------------------------------------------------


def test_render_codex_config_block_carries_skills_table():
    out = render_codex_config_block()
    assert CODEX_CONFIG_SENTINEL_START in out
    assert CODEX_CONFIG_SENTINEL_END in out
    assert "[skills]" in out
    assert "include_instructions = false" in out


def test_user_has_unmanaged_skills_table_detects_outside_sentinels():
    text = "[skills]\ninclude_instructions = true\n"
    assert _user_has_unmanaged_skills_table(text) is True


def test_user_has_unmanaged_skills_table_ignores_managed_block():
    text = (
        f"{CODEX_CONFIG_SENTINEL_START}\n"
        "[skills]\n"
        "include_instructions = false\n"
        f"{CODEX_CONFIG_SENTINEL_END}\n"
    )
    assert _user_has_unmanaged_skills_table(text) is False


def test_install_codex_config_toml_creates_when_missing(tmp_path):
    cfg = tmp_path / "config.toml"
    assert not cfg.exists()
    _install_codex_config_toml(str(cfg))
    body = cfg.read_text()
    assert CODEX_CONFIG_SENTINEL_START in body
    assert "[skills]" in body
    assert "include_instructions = false" in body


def test_install_codex_config_toml_appends_alongside_user_keys(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "model = \"gpt-5\"\n\n"
        "[mcp_servers.linear]\n"
        "url = \"https://mcp.example\"\n"
    )
    _install_codex_config_toml(str(cfg))
    body = cfg.read_text()
    assert "model = \"gpt-5\"" in body  # user keys preserved
    assert "[mcp_servers.linear]" in body
    assert CODEX_CONFIG_SENTINEL_START in body
    assert "include_instructions = false" in body


def test_install_codex_config_toml_is_idempotent(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("model = \"gpt-5\"\n")
    _install_codex_config_toml(str(cfg))
    after_first = cfg.read_text()
    _install_codex_config_toml(str(cfg))
    after_second = cfg.read_text()
    assert after_first == after_second


def test_install_codex_config_toml_refuses_with_existing_user_skills_table(
    tmp_path, capsys
):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[skills]\n"
        "# user-managed\n"
        "include_instructions = true\n"
    )
    before = cfg.read_text()
    _install_codex_config_toml(str(cfg))
    after = cfg.read_text()
    # The file must not be mutated when a user-owned [skills] table exists.
    assert after == before
    err = capsys.readouterr().err
    assert "user-owned [skills] table" in err
    assert "include_instructions = false" in err


def test_uninstall_codex_config_toml_strips_only_managed_block(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "model = \"gpt-5\"\n\n"
        "[mcp_servers.linear]\n"
        "url = \"https://mcp.example\"\n"
    )
    _install_codex_config_toml(str(cfg))
    _uninstall_codex_config_toml(str(cfg))
    body = cfg.read_text()
    assert "model = \"gpt-5\"" in body
    assert "[mcp_servers.linear]" in body
    assert CODEX_CONFIG_SENTINEL_START not in body
    assert "include_instructions" not in body


def test_uninstall_codex_config_toml_deletes_file_when_block_was_only_content(
    tmp_path,
):
    cfg = tmp_path / "config.toml"
    _install_codex_config_toml(str(cfg))
    assert cfg.exists()
    _uninstall_codex_config_toml(str(cfg))
    # File should be gone — leaving an empty config.toml could confuse codex.
    assert not cfg.exists()


def test_run_install_writes_codex_config_when_not_opted_out(tmp_path):
    cfg = tmp_path / "codex_config.toml"
    rc = tmp_path / "rc"
    hooks = tmp_path / "hooks.json"
    run_install(_args(
        rc_file=str(rc),
        codex_hooks_file=str(hooks),
        print_only=False,
        skills_dir=str(tmp_path / "no-skills"),
        keep_codex_catalog=False,
        codex_config_path=str(cfg),
    ))
    body = cfg.read_text()
    assert "[skills]" in body
    assert "include_instructions = false" in body


def test_run_install_keep_codex_catalog_skips_catalog_but_still_trusts_hooks(tmp_path):
    """--keep-codex-catalog opts out of the [skills] disable, but the
    hook-trust auto-stamp must still run so the hooks fire from session
    one. (Without trust the hooks never fire — that defeats install.)
    """
    cfg = tmp_path / "codex_config.toml"
    rc = tmp_path / "rc"
    hooks = tmp_path / "hooks.json"
    run_install(_args(
        rc_file=str(rc),
        codex_hooks_file=str(hooks),
        print_only=False,
        skills_dir=str(tmp_path / "no-skills"),
        keep_codex_catalog=True,
        codex_config_path=str(cfg),
    ))
    body = cfg.read_text() if cfg.exists() else ""
    # Catalog disable is opted-out:
    assert CODEX_CONFIG_SENTINEL_START not in body
    assert "include_instructions = false" not in body
    # Hook trust auto-stamp is still applied:
    assert CODEX_TRUST_SENTINEL_START in body
    assert "trusted_hash" in body


# --------------------------------------------------------------------------
# Codex hook-trust auto-stamp (avoids manual /hooks step)
# --------------------------------------------------------------------------


def test_render_codex_trust_block_stamps_managed_hooks(tmp_path):
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [{
                "_mega_tron_managed": "0.5.0",
                "matcher": ".*",
                "hooks": [{"type": "command", "command": "mega-tron hook"}],
            }],
            "Stop": [{
                "_mega_tron_managed": "0.5.0",
                "matcher": ".*",
                "hooks": [{"type": "command", "command": "mega-tron stop-hook"}],
            }],
        }
    }))
    hooks_data = json.loads(hooks.read_text())
    block = render_codex_trust_block(hooks, hooks_data)
    assert CODEX_TRUST_SENTINEL_START in block
    assert CODEX_TRUST_SENTINEL_END in block
    # Both events trust-stamped under [hooks.state."<canon>:<event>:0:0"]
    canon = str(hooks.resolve())
    assert f'[hooks.state."{canon}:user_prompt_submit:0:0"]' in block
    assert f'[hooks.state."{canon}:stop:0:0"]' in block
    # Each carries a sha256: hash.
    assert "trusted_hash = \"sha256:" in block


def test_install_codex_trust_appends_block_into_config_toml(tmp_path):
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [{
                "_mega_tron_managed": "0.5.0",
                "matcher": ".*",
                "hooks": [{"type": "command", "command": "mega-tron hook"}],
            }]
        }
    }))
    cfg = tmp_path / "config.toml"
    cfg.write_text("model = \"gpt-5\"\n")
    _install_codex_trust(str(cfg), hooks)
    body = cfg.read_text()
    assert "model = \"gpt-5\"" in body  # user content preserved
    assert CODEX_TRUST_SENTINEL_START in body
    assert "trusted_hash" in body


def test_install_codex_trust_is_idempotent(tmp_path):
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({
        "hooks": {
            "Stop": [{
                "_mega_tron_managed": "0.5.0",
                "matcher": ".*",
                "hooks": [{"type": "command", "command": "mega-tron stop-hook"}],
            }]
        }
    }))
    cfg = tmp_path / "config.toml"
    _install_codex_trust(str(cfg), hooks)
    first = cfg.read_text()
    _install_codex_trust(str(cfg), hooks)
    assert cfg.read_text() == first


def test_install_codex_trust_skips_unmanaged_hooks(tmp_path):
    """Trust auto-stamp must never trust hooks the user wrote by hand."""
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [{
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "user-cmd"}],
            }]
        }
    }))
    cfg = tmp_path / "config.toml"
    _install_codex_trust(str(cfg), hooks)
    # No managed entries → no block written → cfg may be empty/missing.
    if cfg.exists():
        assert CODEX_TRUST_SENTINEL_START not in cfg.read_text()


def test_install_codex_trust_dedupes_codex_native_entries(tmp_path):
    """Codex itself writes ``[hooks.state."<hooks.json>:event:0:0"]`` entries
    when the user accepts a managed hook via the ``/hooks`` trust UI. If we
    then append our sentinel-fenced block carrying the same keys, the
    resulting config.toml has duplicate table headers and codex rejects it
    with a ``duplicate key`` TOML parse error — every ``codex exec``
    returns rc=1 until the duplicate is removed. The installer must
    detect any unfenced matching entry and strip it before writing our
    block; the trust hash on both sides is identical so this is safe.
    """
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [{
                "_mega_tron_managed": "0.5.0",
                "matcher": ".*",
                "hooks": [{"type": "command", "command": "mega-tron hook"}],
            }],
            "Stop": [{
                "_mega_tron_managed": "0.5.0",
                "matcher": ".*",
                "hooks": [{"type": "command", "command": "mega-tron stop-hook"}],
            }],
        }
    }))
    cfg = tmp_path / "config.toml"
    # Simulate codex having already auto-stamped its own (unfenced) trust
    # entries for the same keys — this is the failure scenario.
    cfg.write_text(
        'model = "gpt-5"\n\n'
        f'[hooks.state."{hooks}:user_prompt_submit:0:0"]\n'
        'trusted_hash = "sha256:legacy"\n\n'
        f'[hooks.state."{hooks}:stop:0:0"]\n'
        'trusted_hash = "sha256:legacy-stop"\n\n'
        '[unrelated]\n'
        'keep = "me"\n'
    )
    _install_codex_trust(str(cfg), hooks)
    body = cfg.read_text()
    # User content untouched.
    assert 'model = "gpt-5"' in body
    assert '[unrelated]\nkeep = "me"' in body
    # Managed block present.
    assert CODEX_TRUST_SENTINEL_START in body
    # And exactly ONE entry per event key — no duplicates that would
    # break TOML parsing.
    assert body.count(f'[hooks.state."{hooks}:user_prompt_submit:0:0"]') == 1
    assert body.count(f'[hooks.state."{hooks}:stop:0:0"]') == 1


def test_install_codex_trust_does_not_touch_unrelated_hookstate(tmp_path):
    """Another plugin's hook-trust entries (for a different hooks.json path)
    must survive the dedup pass — we only strip entries that collide with
    OUR hooks.json keys."""
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [{
                "_mega_tron_managed": "0.5.0",
                "matcher": ".*",
                "hooks": [{"type": "command", "command": "mega-tron hook"}],
            }]
        }
    }))
    cfg = tmp_path / "config.toml"
    # An unrelated plugin's hook-trust entry sitting in the same file.
    other_hooks = tmp_path / "other-plugin" / "hooks.json"
    cfg.write_text(
        f'[hooks.state."{other_hooks}:user_prompt_submit:0:0"]\n'
        'trusted_hash = "sha256:other-plugin"\n'
    )
    _install_codex_trust(str(cfg), hooks)
    body = cfg.read_text()
    # The other plugin's trust entry stays.
    assert f'[hooks.state."{other_hooks}:user_prompt_submit:0:0"]' in body
    assert 'sha256:other-plugin' in body
    # Our managed block is also written.
    assert CODEX_TRUST_SENTINEL_START in body


def test_uninstall_codex_config_toml_strips_both_blocks(tmp_path):
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [{
                "_mega_tron_managed": "0.5.0",
                "matcher": ".*",
                "hooks": [{"type": "command", "command": "mega-tron hook"}],
            }]
        }
    }))
    cfg = tmp_path / "config.toml"
    cfg.write_text("model = \"gpt-5\"\n")
    _install_codex_config_toml(str(cfg))
    _install_codex_trust(str(cfg), hooks)
    body_before = cfg.read_text()
    assert CODEX_CONFIG_SENTINEL_START in body_before
    assert CODEX_TRUST_SENTINEL_START in body_before
    _uninstall_codex_config_toml(str(cfg))
    body_after = cfg.read_text()
    assert "model = \"gpt-5\"" in body_after
    assert CODEX_CONFIG_SENTINEL_START not in body_after
    assert CODEX_TRUST_SENTINEL_START not in body_after


def test_run_install_writes_trust_block_alongside_catalog_disable(tmp_path):
    """End-to-end: running install with hooks enabled also stamps trust."""
    cfg = tmp_path / "codex_config.toml"
    hooks = tmp_path / "hooks.json"
    rc = tmp_path / "rc"
    run_install(_args(
        rc_file=str(rc),
        codex_hooks_file=str(hooks),
        print_only=False,
        skills_dir=str(tmp_path / "no-skills"),
        keep_codex_catalog=False,
        codex_config_path=str(cfg),
    ))
    body = cfg.read_text()
    assert CODEX_CONFIG_SENTINEL_START in body  # catalog disabled
    assert CODEX_TRUST_SENTINEL_START in body   # hooks trusted
    # Both events stamped.
    assert "user_prompt_submit:0:0" in body
    assert "stop:0:0" in body


def test_install_no_hook_flag_skips_hooks_json(tmp_path):
    hooks_file = tmp_path / "hooks.json"
    rc_file = tmp_path / "rc"
    run_install(_args(
        rc_file=str(rc_file),
        codex_hooks_file=str(hooks_file),
        print_only=False,
        skills_dir=str(tmp_path / "nope"),
        no_hook=True,
    ))
    assert not hooks_file.exists()
    assert "codex() {" in rc_file.read_text()


# --------------------------------------------------------------------------
# rc-file uninstall (existing)
# --------------------------------------------------------------------------


def test_install_uninstall_preserves_surrounding_lines(tmp_path):
    rc = tmp_path / "rc"
    rc.write_text("# before\nexport FOO=1\n")
    run_install(
        _args(
            rc_file=str(rc),
            print_only=False,
            skills_dir=str(tmp_path / "nonexistent"),
        )
    )
    assert "codex() {" in rc.read_text()
    run_install(_args(rc_file=str(rc), uninstall=True))
    rest = rc.read_text()
    assert "codex() {" not in rest
    assert "export FOO=1" in rest


def _have_bash() -> bool:
    return subprocess.run(
        ["bash", "-c", "true"], capture_output=True
    ).returncode == 0


@pytest.mark.skipif(not _have_bash(), reason="bash not on PATH")
def test_wrapper_routes_through_fake_codex(tmp_path):
    """End-to-end: source the wrapper, invoke `codex exec --prompt`, assert
    a fake codex receives CODEX_HOME and a staged prompt."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    log = tmp_path / "fake_codex.log"
    # Log each arg on its own line so we can verify newlines survived the
    # `$(...)` capture inside the wrapper (bash strips trailing newlines
    # from command substitution — regression test for that).
    fake_codex.write_text(
        f"""#!/usr/bin/env bash
{{
  echo "CODEX_HOME=$CODEX_HOME"
  for a in "$@"; do
    echo "--- arg ---"
    echo "$a"
  done
}} >> {log}
"""
    )
    fake_codex.chmod(0o755)

    # Fake mega-tron that emits a known prefix. Mirrors the
    # non-forcing candidate-skills format produced by the real CLI
    # (no `$` token — that would trigger codex's MUST-use rule).
    fake_msr = fake_bin / "mega-tron"
    fake_msr.write_text(
        """#!/usr/bin/env bash
echo -n "Candidate skills for this task: jwt-verifier. Use whichever fit; ignore the rest.

"
"""
    )
    fake_msr.chmod(0o755)

    snippet = render_snippet(
        skills_dir=str(tmp_path / "skills"),
        codex_home=str(tmp_path / "codex-home"),
        top_k=3,
        budget_tok=500,
    )
    rc = tmp_path / "wrapper.sh"
    rc.write_text(snippet)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["MEGA_QUIET"] = "1"

    cmd = f"source {rc} && codex exec --prompt 'do thing X'"
    result = subprocess.run(
        ["bash", "-c", cmd],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    log_text = log.read_text()
    assert "CODEX_HOME=" in log_text
    assert str(tmp_path / "codex-home") in log_text
    assert "jwt-verifier" in log_text  # the fake msr's prefix made it through
    assert "do thing X" in log_text
    # Regression: the wrapper must restore the `\n\n` separator that
    # `$(...)` strips, so prefix + prompt don't render glued together
    # (`rest.do thing X`). We re-emit each arg on its own line above and
    # check the prompt arg has a blank line between prefix and prompt.
    prompt_arg = log_text.split("--- arg ---")[-1]
    assert "ignore the rest.\n\ndo thing X" in prompt_arg


@pytest.mark.skipif(not _have_bash(), reason="bash not on PATH")
def test_wrapper_bypass_with_mega_router_zero(tmp_path):
    """MEGA_ROUTER=0 must skip the staging step entirely."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "fake_codex.log"
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        f"""#!/usr/bin/env bash
echo "CODEX_HOME=${{CODEX_HOME:-unset}}" >> {log}
echo "ARGS=$*" >> {log}
"""
    )
    fake_codex.chmod(0o755)

    # If MEGA_ROUTER=0 fails to bypass, this script would be called and fail the test.
    fake_msr = fake_bin / "mega-tron"
    fake_msr.write_text("""#!/usr/bin/env bash
echo "should-not-run" >&2
exit 99
""")
    fake_msr.chmod(0o755)

    snippet = render_snippet(
        skills_dir="/x", codex_home="/y", top_k=3, budget_tok=500
    )
    rc = tmp_path / "wrapper.sh"
    rc.write_text(snippet)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["MEGA_ROUTER"] = "0"

    result = subprocess.run(
        ["bash", "-c", f"source {rc} && codex exec --prompt 'task'"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    log_text = log.read_text()
    # CODEX_HOME must NOT have been overridden by the wrapper.
    assert "CODEX_HOME=unset" in log_text
    # And the prompt arrived without any prefix prepended.
    assert "--prompt task" in log_text


# --- Legacy mega-skill-router auto-cleanup --------------------------------


def test_strip_legacy_mega_skill_router_blocks(monkeypatch, tmp_path):
    """mega-skill-router (v0.5.0) was the first-generation name; its
    managed AGENTS.md / shell-rc blocks point at a binary that's no
    longer on any system we ship to. `mega-tron install` should
    auto-strip these blocks on every run so the model never sees
    guidance pointing at a `command not found`.
    """
    from importlib import reload
    import mega_tron.cli.install as install_mod

    fake_home = tmp_path
    (fake_home / ".codex").mkdir()
    agents_md = fake_home / ".codex" / "AGENTS.md"
    agents_md.write_text(
        "# AGENTS.md\n\nOther guidance.\n\n"
        "<!-- >>> mega-skill-router (managed; edit between sentinels at your own risk) >>> -->\n"
        "<!-- mega-skill-router v0.5.0 -->\n"
        "## Skill routing (mega-skill-router)\n\n"
        "You have access to `mega-skill-router search ...`.\n"
        "<!-- <<< mega-skill-router <<< -->\n"
        "\nTrailing content.\n"
    )
    # Shell-rc form too
    zshrc = fake_home / ".zshrc"
    zshrc.write_text(
        "export PS1='%~'\n\n"
        "# >>> mega-skill-router (managed; edit between sentinels at your own risk) >>>\n"
        "# mega-skill-router shell wrapper\n"
        "codex() { echo legacy; }\n"
        "# <<< mega-skill-router <<<\n"
        "\nalias ll='ls -la'\n"
    )

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    reload(install_mod)

    edited = install_mod._strip_legacy_mega_skill_router_blocks()
    assert set(edited) == {agents_md, zshrc}

    agents_md_text = agents_md.read_text()
    assert "mega-skill-router" not in agents_md_text
    assert "Other guidance." in agents_md_text
    assert "Trailing content." in agents_md_text

    zshrc_text = zshrc.read_text()
    assert "mega-skill-router" not in zshrc_text
    assert "export PS1" in zshrc_text
    assert "alias ll=" in zshrc_text


def test_strip_legacy_idempotent(monkeypatch, tmp_path):
    """Second call on already-clean files must be a no-op."""
    from importlib import reload
    import mega_tron.cli.install as install_mod

    fake_home = tmp_path
    (fake_home / ".codex").mkdir()
    agents_md = fake_home / ".codex" / "AGENTS.md"
    agents_md.write_text("# AGENTS.md\n\nClean file, no legacy.\n")

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    reload(install_mod)

    first = install_mod._strip_legacy_mega_skill_router_blocks()
    second = install_mod._strip_legacy_mega_skill_router_blocks()
    assert first == []
    assert second == []


# --- Persistent .md contract regression -----------------------------------


def test_all_host_install_templates_use_inline_verdict_contract():
    """The .md guidance installed under ~/.claude/CLAUDE.md,
    ~/.codex/AGENTS.md, and ~/.gemini/GEMINI.md must teach the new
    inline-verdict contract — not the old 2-phase Stop-hook protocol.
    The .md files survive compaction (the host CLI re-injects them
    every turn), so a stale contract here drifts the model's behavior
    even after the BeforeAgent / UserPromptSubmit hook stops firing
    after turn 1.
    """
    import mega_tron.hosts.claude_code.install as claude_install
    import mega_tron.hosts.codex.install as codex_install
    import mega_tron.hosts.gemini_cli.install as gemini_install

    for mod in (claude_install, codex_install, gemini_install):
        # Each install module pins its own CLAUDE.md / AGENTS.md /
        # GEMINI.md block as a module-level string constant. Find it.
        block_src = None
        for attr in dir(mod):
            val = getattr(mod, attr)
            if isinstance(val, str) and "skill-used" in val and "mega-tron" in val.lower():
                block_src = val
                break
        assert block_src, f"{mod.__name__}: could not find managed .md block"
        # The new contract: inline verdict attribute + silence option +
        # no follow-up evaluation turn.
        assert 'verdict="HELPFUL|HARMFUL|NEUTRAL"' in block_src, mod.__name__
        assert "Silence" in block_src or "silence" in block_src, mod.__name__
        assert (
            "no follow-up" in block_src or "no continuation" in block_src
        ), mod.__name__
        # `reason` attribute must explicitly require English. Reasons
        # land in SKILL.md + the verdict-embedding store, where the
        # embedder is tuned on English; mixing languages silently
        # degrades future routing. The clause has to be in the .md
        # template (not just the BeforeAgent hook context) so it
        # survives compaction and stays in the model's system prompt.
        assert "must be written in English" in block_src or "must\nbe written in English" in block_src, mod.__name__
