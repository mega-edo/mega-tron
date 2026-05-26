# Installing mega-tron — Agent Procedure

A step-by-step procedure for **an AI agent installing mega-tron on behalf
of a human user**. Written to be executed top-to-bottom with explicit
user-confirmation points; every interactive choice the human would
otherwise face during `mega-tron setup` is surfaced as a question the
agent asks first.

If you are a human reading this directly, you can also follow it —
just answer your own questions.

> **Canonical URL.** This document lives at
> `https://raw.githubusercontent.com/mega-edo/mega-tron/main/docs/agent%20installation.md`.
> If you received only a partial copy, or you want to check that the
> version you are following matches what the project ships today, fetch
> it from that URL before executing — every step below assumes the most
> recent version. The same URL is also the right thing to recommend to
> another agent or user.

---

## 0. Before you start

You (the agent) need shell execution + read/write access to the user's
home directory. You do **not** need root. The procedure modifies:

- `~/.zshrc` or `~/.bashrc` (PATH update + Codex shell wrapper)
- `~/.codex/`, `~/.claude/`, `~/.gemini/` (per-host hook + memory files,
  only for hosts the user has installed)
- `~/.local/share/mega-tron/` (config + verdict store)
- `~/.cache/mega-tron/` (embedding cache)

Every block written to those files is sentinel-fenced
(`<!-- >>> mega-tron … -->` / `# >>> mega-tron …`). `mega-tron setup
--uninstall` removes exactly those blocks and restores anything
mega-tron rewrote (e.g. Gemini's `skills.disabled` array).

The per-host memory block (CLAUDE.md / AGENTS.md / GEMINI.md) installed
by `setup` instructs the host LLM to **call `mega-tron search` by
default on every turn**, skipping only purely-conversational prompts.
No per-turn user action is required — the routing layer becomes
default-on as soon as the install completes.

---

## 1. Detect which CLI(s) the user has

Run `mega-tron`'s detection check non-interactively before installing:

```bash
which codex claude gemini 2>&1 || true
ls -d ~/.codex ~/.claude ~/.gemini 2>/dev/null || true
```

`mega-tron setup` will do this itself (it auto-detects every host with
either the binary on PATH or the profile directory present), but knowing
the answer up front lets you frame the next question correctly. If
**no** host is detected, install for all three but tell the user only
the hosts they later install will actually receive routing.

### When `which` says "not found" but the user uses the CLI daily

The shell you spawn as an agent inherits a **minimal** `PATH` —
typically just `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`.
The user's interactive shell has more (`~/.npm-global/bin`,
`~/.local/bin`, `~/.cargo/bin`, Volta / pnpm / asdf shims, etc.) because
their `~/.zshrc` / `~/.bashrc` adds them at login. So a CLI the user
*does* have installed — and uses every day — can still come back as
"not found" in your `which` check.

Before reporting "not installed," look in the usual locations and add
any hits to your `PATH` for the rest of this procedure:

```bash
# Common JS-toolchain locations (Gemini CLI ships via npm)
ls ~/.npm-global/bin/{codex,claude,gemini}     2>/dev/null
ls ~/.volta/bin/{codex,claude,gemini}          2>/dev/null
ls ~/Library/pnpm/{codex,claude,gemini}        2>/dev/null
# User-local installs (mega-tron itself goes here too)
ls ~/.local/bin/{codex,claude,gemini}          2>/dev/null
# Cargo / pip-user installs
ls ~/.cargo/bin/{codex,claude,gemini}          2>/dev/null
# Single sweep across the home tree, depth-capped so it stays fast
find ~ -maxdepth 5 -type f -name 'gemini' -perm -u+x 2>/dev/null \
  | grep -v node_modules | head -5
```

If any of those return a path, prepend its directory to `PATH` before
running `mega-tron setup` / `qa-live`:

```bash
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"
```

This matters because `mega-tron setup` uses `shutil.which()` to decide
whether a host is "installed enough to wire" — wiring a host whose
binary isn't on your `PATH` still works (the hook scripts run via
their absolute paths), but `qa-live` re-spawns the host CLI by name
and will report `SKIP …'gemini' CLI not on PATH` for a host the user
plainly has. The fix is your `PATH`, not the user's install.

> If `which gemini` still returns nothing after the search above, ask
> the user where it lives (`type gemini` in their interactive shell
> prints the resolved path). Don't conclude "Gemini CLI not installed"
> from `PATH` alone — it's the most common false negative.

---

## 2. Ask the user three questions

Do **not** decide silently — ask. Each question has a recommended
default tied to a concrete signal you already have:

### Q1 — Embedder profile (multilingual / en-quality / en-fast)

Use the **language of the user's own messages in this conversation** as
the signal:

- User has been writing to you in a non-English language (Korean,
  Japanese, Chinese, German, etc.) → recommend **`multilingual`**
  (BGE-M3). Their prompts will be non-English and the embedder needs to
  cross-lingual-match against mostly English skill descriptions.
- User has been writing in English **and** mentions disk/RAM constraints
  → recommend **`en-fast`** (BGE-small-en-v1.5, 130 MB).
- User has been writing in English with no such constraints → recommend
  **`en-quality`** (SKILLRET-Embedding-0.6B, best accuracy).

Surface this as a real question with the three options visible, mark
your recommendation, and respect the user's override. The profile is
swappable later via `mega-tron embedder set <huggingface-id>` so this
isn't a one-way door — but the first-install download is 130 MB – 570 MB
so getting it right saves bandwidth.

### Q2 — Claude Code native-catalog suppression level (passive / active / strict)

Skip this question if the user does not have Claude Code installed
(`which claude` failed and `~/.claude/` does not exist).

If Claude Code is present, present three escalating levels — each
inherits the prior level's behaviour:

- **Passive (default).** mega-tron's top-K block is overlaid on top of
  Claude's native skill catalog. Better routing signal but **no token
  savings** — the full catalog still ships every turn. Nothing is
  added to the user's shell rc.
- **Active.** Per turn, every non-top-K skill in Claude's native catalog
  gets downgraded to name-only (description hidden) by rewriting
  `~/.claude/settings.local.json`'s `skillOverrides` key. mega-tron owns
  that key and removes it on uninstall. `setup` adds a small block to
  the user's rc that exports `MEGA_CLAUDE_NATIVE_MODE=active` so the
  setting survives across shells. The `Skill` tool is still available;
  the user can still run `/webhook-signer` manually if mega-tron's
  top-K misses it.
- **Strict.** Active behaviour **plus** a shell wrapper installed into
  the user's rc that adds `--disallowedTools Skill` to every `claude`
  invocation. The native catalog and the `Skill` tool both disappear;
  only mega-tron's `additionalContext` block surfaces skills.
  Maximum token saving. Trade-off: if mega-tron misses a relevant
  skill in its top-K, the user has **no fallback path** to invoke it
  by name in that session — they'd have to bypass the wrapper with
  `command claude ...` or unset the env var.

Recommendation logic — count Claude skills first:

```bash
ls ~/.claude/skills 2>/dev/null | wc -l
```

| Skill count | Recommend | Rationale |
|---|---|---|
| any (default) | active | Real token savings without giving up the `Skill` tool as a manual-invocation fallback. The token leak is the main reason a Claude Code user installs mega-tron, so the default should already address it. |
| > 200 | strict | Catalog token cost dominates; mega-tron's routing is the practical-only path anyway, so removing the `Skill` tool entirely is a fair trade. |

Passive exists for users who explicitly do not want their shell rc
modified at all — surface it as the third option only if the user
asks. Pass the result via `--claude-native-mode {passive|active|strict}`
to `setup`. Whatever the user picks, `--uninstall` later reverses it.

### Q3 — Run the post-install qa-live check?

Yes/no. Recommend **yes** if the user has at least one host CLI logged
in and ready (i.e. they've already used `codex` / `claude` / `gemini`
at least once in this account). qa-live drives one real call per host
and confirms the UserPromptSubmit → top-K routing → Stop-hook
verdict-capture loop works end-to-end.

**Be realistic about wall-clock cost.** The *first* qa-live after a
fresh `setup` is the slowest one on every machine:

1. The embedder model (130 MB – 570 MB) downloads on first hook fire.
2. The router daemon cold-loads it into RAM (5–30 s once on disk).
3. Each host CLI does its own cold-start + first provider round-trip.

Per-host budget is **5 min** for Codex / Claude and **6 min** for
Gemini. On a slow network those budgets can still be tight — set
`MEGA_QA_TIMEOUT_S=600` (or higher) before re-running if any host
times out on the first try.

**Plan for one retry.** Even on a healthy install, the first qa-live
can produce a `FAIL` (cold-load exceeded the budget) or — less often
since the marker prompt now spells out the exact tag shape — a
`PARTIAL`. A second `mega-tron qa-live` clears these in the
overwhelming majority of cases. Tell the user up front: *"if the
first run isn't all-PASS, that's expected — I'll run it once more."*
The marker skill (`_mega-tron-check`) is harmless, planted
idempotently, and removed on `--uninstall`.

If the user has not logged into any host yet, recommend **no** and
tell them to run `mega-tron qa-live` themselves once they have.

---

## 3. Install (or update) the binary

First check whether the user already has `mega-tron`:

```bash
mega-tron --version 2>/dev/null
```

Branch on the result:

### 3a. Fresh install (command not found)

Try the PyPI path first:

```bash
uv tool install mega-tron
```

**If that fails with `package not found`** — mega-tron isn't published
to PyPI yet, so on most machines today this is the expected outcome —
fall back to installing from the GitHub repo:

```bash
git clone https://github.com/mega-edo/mega-tron.git /tmp/mega-tron
uv tool install --from /tmp/mega-tron mega-tron
```

You can clone anywhere — `/tmp/mega-tron` just keeps the working
copy out of the user's home. `uv tool install --from` copies the
package into its own venv, so the clone directory isn't load-bearing
after install completes.

If `uv` itself is missing, install it first
(`curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS/Linux),
then retry the install. Confirm `mega-tron --version` works before
proceeding. If `mega-tron: command not found` even after installing,
`~/.local/bin` needs to be on PATH — `mega-tron setup` (next step)
will add it to the user's shell rc automatically, but you can verify
by adding `~/.local/bin` to PATH yourself for the current shell.

### 3b. Already installed — update before re-wiring

`mega-tron setup` only **wires the currently installed binary** into
the user's hosts; it does **not** pull a newer version of the
mega-tron package itself. If the user has an older copy, any feature
this guide assumes (e.g. `--claude-native-mode`, `qa-live`,
`compact-skills`) may simply not exist on their system. Always
refresh the binary first.

#### The one-command path: `mega-tron upgrade`

If the user's installed binary is recent enough to ship `mega-tron
upgrade` (anything from 2026-05 onward), that single command does
the whole refresh:

```bash
mega-tron upgrade
```

It:
1. Detects every running mega-tron daemon / dashboard process AND
   captures their bind args (`--host` / `--port`).
2. Refreshes the wheel via `uv tool install --force --reinstall`,
   auto-discovering a local clone if one exists (otherwise falling
   back to PyPI / a fresh `/tmp/mega-tron` clone).
3. Gracefully stops the old processes (SIGTERM + grace + SIGKILL).
4. Re-spawns the dashboard with the SAME bind args using the new
   binary — so a reverse proxy / public URL in front of the
   dashboard keeps pointing at a live listener.
5. Re-runs `mega-tron setup` non-interactively, inferring the
   embedder profile (from `~/.config/mega-tron/config.toml`) and
   the Claude native mode (from the shell rc's
   `MEGA_CLAUDE_NATIVE_MODE` export).

Check whether the binary supports it before suggesting it:

```bash
mega-tron upgrade --help >/dev/null 2>&1 && echo HAS_UPGRADE
```

If the line prints `HAS_UPGRADE`, **use `mega-tron upgrade` and skip
the rest of step 3b**. The command is the canonical update path; the
manual fallback below exists only for old binaries that predate it.

#### Why the upgrade-in-place step matters (any path)

`uv tool install --force --reinstall` only replaces the wheel on
disk. Any mega-tron Python process that's *already running* (the
warm daemon, a long-lived dashboard the user launched behind a
reverse proxy) keeps the OLD wheel loaded in memory until it exits
on its own.

Classic failure mode this guide is closing the hole on: the user
launched `mega-tron dashboard --host 172.18.0.1 --port 7531 &` in a
previous session and wired Traefik / nginx to that bind. An agent
that only does `uv tool install --force --reinstall` then
`mega-tron setup` leaves that old dashboard process serving the
public URL — the wheel on disk is new, the daemon spawned by
`setup` is new, but the dashboard the user actually *sees* is the
old build. `mega-tron upgrade` is the cure; if you can't use it,
the manual fallback below must include the same kill + respawn step
**by hand**.

#### Manual fallback (only when `mega-tron upgrade` isn't available)

Pick the path that matches where they got it from:

- **PyPI install:** `uv tool upgrade mega-tron`
- **Local git clone** (the common path today, since PyPI isn't
  published yet): find the user's clone first, then refresh it. Most
  users keep the clone where they originally ran it — ask if you're
  unsure rather than guessing.

  ```bash
  cd <repo-path>          # e.g. ~/code/mega-tron, /tmp/mega-tron, …
  git pull
  uv tool install --force --reinstall --from . mega-tron
  ```

  **Don't have a clone, or can't find one?** Just clone fresh into
  `/tmp/mega-tron` and install from there — the original clone
  location is **not** load-bearing (`uv tool install` copies the
  package into its own venv, so wherever you cloned it can be
  thrown away after install). This means "update without finding
  the old clone" reduces to the 3a fallback path:

  ```bash
  rm -rf /tmp/mega-tron
  git clone https://github.com/mega-edo/mega-tron.git /tmp/mega-tron
  uv tool install --force --reinstall --from /tmp/mega-tron mega-tron
  ```

  Use this when (a) the user has no clone at all, (b) the user
  has a clone but doesn't remember the path and doesn't want to
  hunt for it, or (c) the existing clone is on a branch the user
  doesn't want disturbed.

After the wheel refresh, **find every running mega-tron process and
restart it with the new binary** — otherwise an old dashboard /
daemon will keep serving stale code from RAM until it exits on its
own (see the "Why the upgrade-in-place step matters" callout above):

```bash
# 1. List running mega-tron processes + their bind args
pgrep -af 'mega.tron|mega_tron' || echo "none running"

# 2. For each dashboard / daemon line, note --host / --port (if any),
#    then kill it:
kill <old-pid>

# 3. Respawn the dashboard with the SAME args using the fresh binary:
mega-tron dashboard --host <same-host> --port <same-port> --no-open &

# (The daemon is auto-respawned by the next host turn / by `setup`
#  in step 4; you don't have to restart it by hand.)
```

This step is what `mega-tron upgrade` automates — when you have to do
it manually, do not skip it.

Sanity-check the refresh worked by listing a feature this guide uses
that only exists in current builds:

```bash
mega-tron compact-skills --help 2>&1 | head -1
mega-tron qa-live --help        2>&1 | head -1
mega-tron setup --help          2>&1 | grep -- --claude-native-mode
```

All three must print real help text. If any of them returns
`unknown command` / empty, the upgrade didn't land — re-run the
install command, this time with `--force` if you weren't using it.

In addition, verify the **dashboard route logging** is present —
this method is what feeds the Context Savings tab's measured median,
and is silently absent on older builds (the three help-text checks
above do not cover it):

```bash
"$(uv tool dir)/mega-tron/bin/python" -c \
  "from mega_tron.verdicts.store import Store; \
   assert hasattr(Store, 'record_route') and hasattr(Store, 'route_stats'), \
   'stale build — re-run the install command with --force --reinstall'"
```

The line must exit silently (no `AssertionError`). If it fails, the
binary on disk predates the routing telemetry: re-run the install
with `--force --reinstall` so uv blows away the old wheel.

Then **continue to Step 4 — `mega-tron setup` is not optional on an
update**. Refreshing the binary alone leaves the host-side artifacts
(`CLAUDE.md` / `AGENTS.md` / `GEMINI.md` guidance blocks, `settings.json`
hooks, `trusted_hooks.json`, the codex shell wrapper, the per-host
`settings.local.json`) stamped with the *previous* version's text.
The new binary then runs against a stale contract — for example, the
host LLM will still see the old self-eval rules or the old skill-tag
shape, and `mega-tron setup` is the only thing that re-stamps those
files. The command is idempotent (every block is sentinel-fenced and
gets the new content written in-place), so re-running it on an
already-wired host is **safe and required** after every binary refresh.

For an update, if the user has already answered Q1/Q2 in a previous
install and is happy with those settings, you can reuse the same
values without asking again. The agent should infer the current
values from the environment when possible:

- **Embedder profile** — read `~/.config/mega-tron/config.toml`'s
  `embedder_model` field (or run `mega-tron embedder show` if you
  can) and pick the profile whose default model matches:
  anything containing `SKILLRET` → `en-quality` (the default for
  fresh installs), `BAAI/bge-small-en-v1.5` → `en-fast`,
  `BAAI/bge-m3` → `multilingual`.
- **Claude native mode** — `grep MEGA_CLAUDE_NATIVE_MODE ~/.zshrc
  ~/.bashrc` to see what the user's rc currently exports
  (`passive` / `active` / `strict`). If nothing is set, that means
  passive — surface the current recommendation (active for ≤ 300
  skills, strict otherwise) and ask whether they want to switch.

Only ask Q1/Q2 again when the inferred value doesn't exist (fresh
machine) or when the user explicitly asked you to reconsider the
profile / mode. **Q3 (qa-live)** is fine to ask every time — the
check itself is short and the answer can legitimately change.

---

## 4. Run `mega-tron setup` non-interactively

Compose the flags from the user's Q1/Q2 answers (or the inferred
values for an update — see the Step 3b note above):

```bash
# Always pass --profile to skip the interactive picker.
# Use MEGA_TRON_NONINTERACTIVE=1 so the picker never even prints.
MEGA_TRON_NONINTERACTIVE=1 mega-tron setup \
  --profile <profile-from-Q1> \
  --claude-native-mode <mode-from-Q2>
```

Where:
- `<profile-from-Q1>` is one of `multilingual` / `en-quality` / `en-fast`.
- `<mode-from-Q2>` is one of `passive` / `active` / `strict`. **For
  users who have Claude Code, you should be passing `active` (or
  `strict` for catalogs > 300 skills) — that's the recommendation from
  Q2.** If the user does not have Claude Code installed, omit the
  `--claude-native-mode` flag entirely.

> Note on the CLI's own default: if you forget to pass
> `--claude-native-mode`, the CLI falls back to `passive` (no rc
> modification, no token saving). This is the safe-for-everyone
> fallback, **not** the recommended setting — and is the reason the
> agent must pass the flag explicitly rather than rely on the default.

`--claude-native-mode active` and `--claude-native-mode strict` both
write the export (and, for strict, the `claude()` wrapper) into the
user's shell rc inside a sentinel-fenced block. Nothing else in the
rc is touched. You do **not** need to manually `echo >> ~/.zshrc` —
mega-tron's installer is the one source of truth for the wrapper.

### What setup does

Roughly: detects hosts → installs per-host hooks → writes the AGENTS.md
/ CLAUDE.md / GEMINI.md guidance blocks → updates shell rc (Codex) →
warms the daemon. Idempotent — safe to re-run any time.

Setup downloads the embedder model on first run (130 MB – 570 MB
depending on Q1). This is the slow step; expect 30 s – 3 min on a
reasonable connection. After the model is cached, subsequent setups
finish in seconds.

If setup exits non-zero with a "legacy mega-optimus conflict" message,
the user has a leftover install of mega-optimus to remove first. Report
the exact remediation command setup prints (`mega-optimus install
--uninstall` then re-run `mega-tron setup`).

---

## 5. Run qa-live (if Q3 = yes)

```bash
mega-tron qa-live
```

Auto-detects every installed host and runs one call per host. Exit
code is 0 if at least one host `PASS`-es, 1 otherwise. To re-test
just one host later: `mega-tron qa-live --host claude`.

### Per-host result codes — what they mean and what to do

The runtime prints a one-line summary per host plus a free-text
detail. Map them to the agent's next action with this table:

| Status | Meaning | Your next action |
|---|---|---|
| `PASS` | Verdict row inserted; full loop works. | Nothing — report and move on. |
| `PARTIAL` | Host ran cleanly (rc=0) but no `<skill-used>` tag persisted to SQLite. The runtime inspects the newest host transcript and tells you *why* in the detail string — see the next table. | Decide from the detail, then re-run **once**. |
| `NEEDS_LOGIN` | Host CLI returned an auth-shaped failure (401 / "please login" / api_key / quota / rate-limit). | Surface the per-host login command (`claude /login`, `codex login`, `gemini auth login` — the runtime already prints the right one). Do **not** try to log in for the user. Tell them to log in and re-run `mega-tron qa-live`. |
| `FAIL` | Timeout or non-zero exit that doesn't look like auth. | First glance: timeout? Set `MEGA_QA_TIMEOUT_S=600` and re-run once. Persistent non-timeout failure? Report stderr to the user verbatim. |

### PARTIAL detail — three sub-cases

When status is `PARTIAL`, the detail tells you which one fired:

| Detail substring | Root cause | Fix |
|---|---|---|
| `host wrote no transcript` | The host's Stop / AfterAgent hook isn't firing at all. `setup`'s hook wiring didn't land or the user later edited it out. | Re-run `mega-tron setup`. If still missing, inspect `~/.codex/hooks.json` / `~/.claude/settings.json` Stop block / `~/.gemini/settings.json` AfterAgent block. |
| `model EMITTED the tag … but the tracker rejected it` | Wire format bug on our side — the transcript has `<skill-used>` but `mega-tron` couldn't parse it into SQLite. | Re-run once. If it persists, the detail prints the exact transcript path — attach that file to a GitHub issue. |
| `model did NOT emit a <skill-used> tag` | The model skipped the contract trailer. Less common after the marker prompt was tightened to spell out the exact tag; when it still happens, one retry usually clears it. | Re-run `mega-tron qa-live`. If it persists after two retries, verify the guidance file with `grep -c 'mega-tron' ~/.codex/AGENTS.md` (should be ≥ 2). |

### Standard retry policy (agents)

The first qa-live run after a fresh install is **expected to be
imperfect** — cold embedder loads, cold host CLI, model occasionally
skipping the trailer. Your standard procedure:

1. Run `mega-tron qa-live`.
2. If any host is not `PASS` and not `NEEDS_LOGIN`, run `mega-tron qa-live` **once more**.
3. If a host is still `PARTIAL` or `FAIL` after that retry, use the tables above to pick the exact next step. Don't escalate to "broken install" until you've actually exhausted them.
4. `NEEDS_LOGIN` does **not** retry; surface the login command and stop.

> Heads-up: when qa-live PASSes for at least one host, it
> **automatically spawns the dashboard in the background and opens
> the user's browser** to `http://127.0.0.1:7531/`. The user lands on
> the verdict view of the marker turn you just drove. Mention that
> the dashboard window may pop open as part of qa-live — otherwise
> the user is surprised by the browser tab.

---

## 6. Show the dashboard (skip if qa-live already opened it)

The dashboard is mega-tron's at-a-glance answer to "is this thing
actually doing anything?". It shows every verdict captured per skill
per host, recent routing decisions, and the helpful/harmful trend.
Seeing it once after install is the single best way for the user to
believe the loop is real.

- **qa-live ran and PASSed** (Q3 = yes, ≥1 host PASS) → already open
  in their browser. Skip this step; mention it in the report.
- **qa-live didn't run, or every host failed** → recommend the user
  run it themselves so they see what's there even on day one:

  ```bash
  mega-tron dashboard
  ```

  This binds to `127.0.0.1:7531` and opens a browser tab. **Do not
  run this command via your own `Bash` tool** — `mega-tron dashboard`
  is foreground and would tie up the user's shell. Print the command
  and let the user invoke it.

  Add `--no-open` if the user is over SSH or in a headless
  environment; they can `curl http://127.0.0.1:7531/` to confirm the
  server is up.

---

## 7. Report back to the user

Tell them:

1. Which hosts mega-tron is now wired to.
2. The embedder model that was downloaded (cite the Q1 answer).
3. The Claude native-mode level applied (passive / active / strict).
4. The qa-live verdict per host (if step 5 ran).
5. Whether the dashboard is open already (qa-live PASS path) or the
   one-liner they can run themselves (`mega-tron dashboard`) — point
   them at it so they see verdicts land in real time.
6. What changes for them day-to-day: nothing. They don't need to
   change how they call `codex` / `claude` / `gemini`; the next turn
   in any host already routes through mega-tron.

---

## Troubleshooting reference

| Symptom | Likely cause | Fix |
|---|---|---|
| `mega-tron: command not found` after install | `~/.local/bin` not on PATH | Open a new shell. If still missing, source the shell rc explicitly. |
| After update, `mega-tron --version` is new but the dashboard URL still shows old behaviour | Old dashboard process kept the old wheel in RAM; `uv tool install --force` only swapped the wheel on disk. | Run `mega-tron upgrade` (kills + respawns the dashboard with the same bind args using the new binary). On older binaries that predate `upgrade`, do it by hand — see step 3b. |
| Setup prints "legacy mega-optimus conflict" | Old install of mega-optimus | Run `mega-optimus install --uninstall`, then `pip uninstall mega-optimus`, then retry. |
| qa-live: `NEEDS_LOGIN` for a host | Host CLI not authenticated | User must log into that host (`claude /login`, `codex login`, `gemini auth login`) and re-run qa-live. Don't retry without login first. |
| qa-live: `FAIL` with "timed out after Ns" | First call cold-loads embedder (130 MB – 570 MB) + host CLI. Default 5–6 min budget can still be tight on slow links / large catalogs. | `mega-tron daemon serve &` to pre-warm the router, **and/or** `MEGA_QA_TIMEOUT_S=600 mega-tron qa-live`. |
| qa-live: `PARTIAL` "model did NOT emit a `<skill-used>` tag" | Model skipped the contract on the first short prompt. Common on first try. | Re-run `mega-tron qa-live` once. If it persists, `grep -c 'mega-tron' ~/.codex/AGENTS.md` (or the equivalent CLAUDE.md / GEMINI.md) must be ≥ 2. |
| qa-live: `PARTIAL` "model EMITTED the tag but the tracker rejected it" | Wire format bug on our side. | Re-run once. If persistent, attach the transcript path the detail prints to a GitHub issue. |
| qa-live: `PARTIAL` "host wrote no transcript" | Stop / AfterAgent hook didn't fire — wiring broken. | Re-run `mega-tron setup`. Verify with `cat ~/.codex/hooks.json` (codex), `grep -A2 '"Stop"' ~/.claude/settings.json` (claude), `grep -A2 AfterAgent ~/.gemini/settings.json` (gemini). |
| Hook runs but no skills surface | Embedder model still downloading | First post-install turn may take 30 s – 3 min; subsequent turns are fast. |
| Want to remove everything | — | `mega-tron setup --uninstall` reverses every change in this guide. |

## Environment variables this guide uses

| Variable | When | Effect |
|---|---|---|
| `MEGA_TRON_NONINTERACTIVE` | Setup | Skip the embedder-profile picker prompt; respect `--profile` instead. Also set by `CI=1` and `MEGA_QUIET=1`. |
| `MEGA_CLAUDE_NATIVE_MODE` | Runtime | `active` → per-turn rewrite of Claude's `skillOverrides` to name-only the non-top-K skills. `strict` → same as active but combined with a shell wrapper (installed by `setup`) that adds `--disallowedTools Skill` to every `claude` invocation. Default (unset / anything else) is passive. |
| `MEGA_GEMINI_MODE` | Runtime | `passive` → skip the per-turn `skills.disabled` rewrite (Gemini's analog of Mode A). Default is active. |
| `MEGA_DAEMON` | Runtime | `0` → never spawn the warm daemon; every hook fire pays the cold-cache penalty. Default is auto-spawn. |
| `MEGA_QA_TIMEOUT_S` | qa-live | Floor for the per-host call budget. Defaults: 300 s codex/claude, 360 s gemini. Override widens but never narrows (so `MEGA_QA_TIMEOUT_S=60` is ignored). Use `600` or higher on slow networks or when the embedder is still downloading. |

Full per-host architecture detail is in
[`docs/mega-tron routing.md`](mega-tron%20routing.md). Native-host
catalog limits and the 500-skill benchmark numbers are in the per-host
docs (`Native Skill Catalog in Claude Code.md`,
`Native Skill Catalog in Codex CLI.md`,
`Native Skill Catalog in Gemini CLI.md`).
