"""Compute codex's hook-trust hashes so `install` can auto-trust the
``mega-tron`` managed hooks without forcing the user to run the
``/hooks`` slash command on first launch.

Codex 2026 (``codex-rs`` 0.130+) gates user-supplied hooks behind a
trust handshake: any hook discovered in ``~/.codex/hooks.json`` lands
in the ``Untrusted`` state and silently does not fire until the user
trusts it interactively. The trust state is keyed on a SHA-256 hash
of the *normalized* hook identity (event + handler), stored in
``config.toml`` at ``[hooks.state."<key>"].trusted_hash``.

This module reproduces codex's hash algorithm 1:1 (verified against
``codex app-server hooks/list``'s ``currentHash`` field — see
``tests/test_hook_trust.py``):

1. Build a *normalized* identity dict:

       {
         "event_name": "user_prompt_submit" | "stop" | ...,
         "hooks": [
           {
             "async":   False,
             "command": "<the exact command string from hooks.json>",
             "timeout": 600,
             "type":    "command",
           }
         ]
       }

   Notes:
   - ``matcher`` is omitted entirely. Codex normalizes the default
     ``".*"`` matcher to ``None``, which serializes away.
   - All optional handler fields (``commandWindows``, ``timeoutSec``
     override, ``statusMessage``) collapse to their defaults when not
     set; codex hashes the normalized form.
   - ``async`` and ``timeout`` are non-Option in the Rust struct so
     their defaults (``false`` and ``600`` seconds) always appear.

2. Canonicalize the JSON: recursively sort object keys, no whitespace.

3. SHA-256 the bytes, hex-encode, prefix with ``sha256:``.

The trust-state key is::

    <canonical_abs_hooks_json_path>:<event_label_snake>:<group_idx>:<handler_idx>

where the path is what ``Path.resolve()`` returns (codex uses Rust
``std::fs::canonicalize`` which symlink-resolves; on macOS
``/tmp/...`` becomes ``/private/tmp/...``).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


# Codex event-name mapping. The wire format uses PascalCase
# (``UserPromptSubmit``, ``Stop``) in ``hooks.json`` but snake_case
# (``user_prompt_submit``, ``stop``) inside the trust-state key and the
# hash identity. See
# ``codex-rs/hooks/src/lib.rs::hook_event_key_label``.
EVENT_LABEL: dict[str, str] = {
    "PreToolUse": "pre_tool_use",
    "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "SessionStart": "session_start",
    "UserPromptSubmit": "user_prompt_submit",
    "Stop": "stop",
}

DEFAULT_TIMEOUT_SEC = 600


def _canonical(obj):
    if isinstance(obj, dict):
        return {k: _canonical(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [_canonical(x) for x in obj]
    return obj


def compute_hook_hash(event_label: str, command: str) -> str:
    """Return codex's ``sha256:<hex>`` for a single command-type hook
    handler with the default matcher / timeout / async settings.

    Args:
        event_label: the snake_case event label (``user_prompt_submit``
            / ``stop`` / ...).
        command: the exact command string as it appears in hooks.json
            (e.g. ``"mega-tron hook"`` or
            ``"/abs/path/to/wrapper hook"``).
    """
    identity = {
        "event_name": event_label,
        "hooks": [
            {
                "async": False,
                "command": command,
                "timeout": DEFAULT_TIMEOUT_SEC,
                "type": "command",
            }
        ],
    }
    payload = json.dumps(_canonical(identity), separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def compute_hook_key(
    hooks_json_path: Path,
    event_label: str,
    group_index: int = 0,
    handler_index: int = 0,
) -> str:
    """Build codex's ``[hooks.state."<key>"]`` lookup key.

    Codex canonicalizes the hooks.json path (resolves symlinks) before
    using it as the key prefix; we mirror that with ``Path.resolve()``.
    """
    canon = hooks_json_path.resolve(strict=False)
    return f"{canon}:{event_label}:{group_index}:{handler_index}"


def render_trust_state_block(
    hooks_json_path: Path,
    hooks_data: dict,
    *,
    only_managed_keys: set[str] | None = None,
) -> str:
    """Render the ``[hooks.state."<key>"]`` TOML block that pre-trusts
    every managed command hook in ``hooks_data``.

    Returns an empty string when the ``hooks_data`` has no command
    handlers we recognize (defensive — never write a useless block).

    Args:
        hooks_json_path: absolute path to ``hooks.json`` on disk.
        hooks_data: the parsed contents of hooks.json (``{"hooks": {...}}``).
        only_managed_keys: when supplied, restrict to entries containing
            one of these keys (e.g. ``{"_mega_tron_managed"}``)
            so we never silently trust a hook the user wrote by hand.
    """
    events = hooks_data.get("hooks", {})
    lines: list[str] = []
    for event_name, entries in events.items():
        event_label = EVENT_LABEL.get(event_name)
        if not event_label:
            continue
        for group_idx, entry in enumerate(entries):
            if only_managed_keys is not None:
                if not any(k in entry for k in only_managed_keys):
                    continue
            for handler_idx, handler in enumerate(entry.get("hooks", [])):
                if handler.get("type") != "command":
                    continue
                command = handler.get("command")
                if not isinstance(command, str):
                    continue
                key = compute_hook_key(
                    hooks_json_path, event_label, group_idx, handler_idx
                )
                trusted_hash = compute_hook_hash(event_label, command)
                lines.append(f'[hooks.state."{key}"]')
                lines.append(f'trusted_hash = "{trusted_hash}"')
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "EVENT_LABEL",
    "compute_hook_hash",
    "compute_hook_key",
    "render_trust_state_block",
]
