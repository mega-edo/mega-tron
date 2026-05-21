# mega-tron Routing — Per-Host Overview

How mega-tron's external router slots into each of the four supported
hosts. The router itself is the same code path everywhere — BGE
embeddings + cosine similarity against the prompt + `mega_meta` ROI
reranking — but each host exposes a different integration seam, so the
**wire format** and **catalog suppression mechanism** differ. This
document is a one-page map; see the per-host `Skill Routing <Host>.md`
files for storage layout, native limits, and benchmark results.

---

## 0. Current Problem

Every host injects its skill catalog **before** seeing the user's
prompt. None of them rank, embed, or top-K filter. That single design
choice produces two distinct failure modes that split the four hosts
into two camps:

- **Camp A — Bounded catalog, lossy truncation (Codex, Claude Code).**
  A hard token budget is imposed on the catalog, but the budget is
  filled without consulting the user's prompt. Codex orders
  alphabetically (SYSTEM-bundled first, then a-z) and truncates
  descriptions mid-sentence when the cap bites. Claude Code keeps
  every name and evicts descriptions by *invocation frequency* —
  "skills you invoke least are dropped first" per the official
  docs — which is *past usage history*, still not prompt-aware.
  Either way the right skill is in the pool but its trigger
  semantics are mangled or invisible, and *neither host ever
  reads the user's current prompt when deciding what to keep*.
- **Camp B — Unbounded catalog, no truncation (Gemini, Hermes).**
  No cap at all. Every discovered skill's `name + description` lands
  in the system prompt verbatim. Signal is preserved, but the user
  pays linear token cost in the pool size — burning enormous context
  on skill-browsing metadata before the user has typed anything.

### The table

| Host        | Camp | Cap on skill catalog                       | What happens at 500 skills                                            | Core problem                                                                                                                            | Catalog tok @ 500 |
|-------------|:----:|--------------------------------------------|-----------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------|------------------:|
| **Codex**   | A    | `min(2% × ctx, 8,000 chars)` — hardcoded   | Descriptions truncated mid-sentence (~223 chars/skill); ~340 skills omitted entirely | **Bounded context window for the catalog, but filled blindly.** Codex never sees the user's prompt before deciding which skills' descriptions to keep — load order alphabetical, not top-K relevance. "USE WHEN: …" trigger phrases get chopped, and overflow skills disappear from implicit selection. |             6,910 |
| **Claude**  | A    | `1% × ctx ≈ 2,000 tokens` (knob: `skillListingBudgetFraction`) | Names retained for all 500; descriptions evicted by *invocation frequency* (least-invoked first) past ~250 skills | **Same root cause as Codex — fixed catalog budget, no prompt-aware ranking.** The cap fraction is user-tunable but the eviction algorithm (least-invoked first) is not, and the user's current prompt is never an input. Past ~250 skills the description disappears entirely and the model sees `password-hash-argon2` next to `password-hash-bcrypt` as bare names with no trigger text. Selection collapses on close neighbours. |             2,572 |
| **Gemini**  | B    | **None.** Every enabled skill injected unconditionally | Catalog grows linearly to 28,140 tokens; on real user pool (1,671 skills) → ~137K tokens / session (~6.9% of 2M context, ~$2 / 5-turn session) | **No catalog cap means the model burns enormous context on browsing.** Every `name + description` of every skill ships in the system prompt before the user has typed anything. Signal is preserved but the signal-to-noise ratio collapses — the right skill sits among 1,670 distractors — and the token bill scales linearly with the pool. |            28,140 |


### Why the two camps share the same root problem

Both camps are symptoms of the same underlying design gap: **the
catalog is built before the user's prompt is known**. Camp A makes
the bill bounded by truncating, which destroys signal. Camp B keeps
the signal by refusing to cap, which destroys the token budget. Neither
camp ranks against the prompt, so both squander capacity on skills the
user will never invoke this turn.

mega-tron's external router replaces the catalog-build step with a
prompt-aware top-K shortlist (BGE cosine + `mega_meta` ROI reweighting)
and injects only ~600 tokens per turn — same router code path on all
four hosts, ~100% F1 across the board at 500 skills (the chapters
below describe the per-host wire format and the catalog-suppression
seam each host exposes).

---

## 1. Codex CLI

- **Hook surface.** `UserPromptSubmit` + `Stop` hooks registered in
  `~/.codex/hooks.json` (Codex keeps hook entries in a *separate*
  JSON file, unlike Claude Code which co-locates them inside
  `settings.json`). Each entry carries a `_mega_tron_managed`
  sentinel key (sibling to the documented `matcher` / `hooks` keys;
  Codex ignores unknown keys, so the marker is invisible to the
  runtime but lets `--uninstall` strip just our entries without
  disturbing user-owned hooks).
- **Hook trust gate.** Codex 2026+ refuses to fire any hook whose
  `(event_name, command)` pair isn't pre-trusted under
  `[hooks.state."<key>"]` in `~/.codex/config.toml`. Untrusted entries
  **silently never fire**. mega-tron's installer stamps the SHA-256
  of each managed `(event_label, command)` into config.toml during
  install, so the user doesn't need to enter `/hooks` interactively.
  The trust block is sentinel-fenced separately from the
  `[skills]` block so `--uninstall` removes it cleanly.
- **Catalog suppression.** `[skills] include_instructions = false`
  in `config.toml` — the only documented kill switch that wholesale
  suppresses Codex's native `<skills_instructions>` block.
  mega-tron flips this so the router owns routing end-to-end with
  zero token overlap against the native truncated catalog. The
  block is sentinel-fenced so `--uninstall` restores the original
  setting.
- **Shell wrapper.** `~/.zshrc` / `~/.bashrc` get a sentinel-fenced
  `codex() { … }` shell function that adds `--include-directories`
  pointing at `mega-tron`'s skill roots before exec'ing the real
  `codex` binary. This is purely a discovery convenience — the hook
  contract works without it — but it lets the user keep mega-tron's
  skill pool outside `~/.codex/skills/` if they want.
- **Prepend wire format.** `additionalContext` stdout JSON emitted by
  the hook; Codex appends it to the developer-role system prompt for
  that turn:
  ```json
  {"hookSpecificOutput": {
     "hookEventName": "UserPromptSubmit",
     "additionalContext": "## Skills (selected for this turn …) …"
  }}
  ```
- **Mention bridge.** Native `$<skill-name>` mention detection
  remains active even with `include_instructions = false` — the
  mention layer is gated on the loaded skill set, not on the
  catalog block — so users who type `$webhook-signer` still get the
  literal-name override path.
- **AGENTS.md guidance.** Sentinel-fenced block in
  `~/.codex/AGENTS.md` (`<!-- >>> mega-tron … >>> -->` …
  `<!-- <<< mega-tron <<< -->`) describes the `<skill-used …/>` tag
  contract the `Stop` hook expects and points the model at
  `mega-tron search` for skills not pre-routed. Codex re-renders
  AGENTS.md into the system prompt every turn, so this guidance
  persists even on second-and-later turns when the hook itself is
  a no-op.
- **Self-evaluation loop.** The first-fire `additionalContext` block
  instructs the model to append one inline tag per skill it relied on
  at the end of its final reply:
  `<skill-used name="..." verdict="HELPFUL|HARMFUL|NEUTRAL" reason="..."/>`.
  The `Stop` hook walks the transcript, pulls every such tag out of the
  final assistant message, and writes verdicts into each skill's
  `mega_meta:` frontmatter silently — stdout stays empty, no
  `decision` field, no retry turn. The `reason` attribute must be
  written in English (it is persisted to SKILL.md and the verdict
  embedding store, which is tuned on English).
- **Wisdom ignite.** Codex is the only host where the hook
  fire-and-forgets a `wisdom_ignite` request to the long-running
  daemon at the *start* of first-fire processing. The MEGA-Code
  curator call (~80 s) gets maximum lead time so a future session's
  prompts benefit when new SKILL.md files land in the
  auto-discovered wisdom dir. This turn still routes off whatever
  is already on disk.

### End-to-end flow

```
┌──────────────────────────────────────────────────────────────────────┐
│  User starts a session: `codex exec "validate this webhook"`         │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  codex turn startup                                                  │
│   • Discover skills across 4 scopes                                  │
│       REPO → walk cwd up to git root, find <root>/.agents/skills/    │
│       USER → ~/.agents/skills/ (and ~/.codex/skills/ legacy)         │
│       ADMIN → /etc/codex/skills/                                     │
│       SYSTEM → bundled (imagegen, openai-docs, …)                    │
│   • Read [skills] include_instructions in ~/.codex/config.toml       │
│       mega-tron has set this to false → native                       │
│       <skills_instructions> block is suppressed                      │
│   • Read [hooks.state."…"] entries; only Trusted ones may fire       │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  UserPromptSubmit hook fires (first turn of the session)             │
│  codex executes the command registered in ~/.codex/hooks.json:       │
│      mega-tron hook                                                  │
│  Hook receives on stdin:                                             │
│      {"session_id":"…","hook_event_name":"UserPromptSubmit",         │
│       "prompt":"validate this webhook", …}                           │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  mega-tron hook (mega_tron.hosts.codex.hook)                         │
│                                                                      │
│   1. First-fire check                                                │
│        marker = $XDG_RUNTIME_DIR/mega-tron/seen-<sid>                │
│        if marker exists → emit `{}` and exit (no-op on later turns;  │
│        persistent guidance lives in AGENTS.md which codex bakes      │
│        into the system prompt every turn)                            │
│                                                                      │
│   2. Wisdom ignite (fire-and-forget)                                 │
│        Send `wisdom_ignite` request to the warm daemon.              │
│        Daemon manages an in-flight pool with dedup; hook doesn't     │
│        wait. New skills materialize for the NEXT session, not this.  │
│                                                                      │
│   3. Daemon-first ranking path (skip if MEGA_DAEMON=0)               │
│        Try Unix-socket query to the cached router daemon. On hit,    │
│        the daemon already has BGE weights + embedding cache hot      │
│        and returns the top-K additional_context block in <50 ms.     │
│        On miss → spawn the daemon detached and fall through.         │
│                                                                      │
│   4. Cold path: embed + rank in-process                              │
│        BGE embedding (cached at ~/.cache/mega-tron/<slug>.npz)       │
│        Router.rank(prompt, top_k)                                    │
│         = cosine(SKILL.md, prompt) ⊔ cosine(name, prompt)            │
│           + mega_meta ROI weight (helpful_count − harmful_count)     │
│           + verdict-store related-skill boost                        │
│         × status_multiplier {active:1.0, suspect:0.5, archived:-1}   │
│                                                                      │
│   5. Render the top-K block:                                         │
│        "## Skills (selected for this turn by mega-tron)              │
│         Strongly prefer activating one of: webhook-signer, …         │
│         - webhook-signer                                             │
│             path: /…/.agents/skills/webhook-signer/SKILL.md          │
│             desc: Validate HMAC-SHA256 webhook signatures.           │
│         - …"                                                         │
│      emit on stdout:                                                 │
│        {"hookSpecificOutput": {                                      │
│           "hookEventName": "UserPromptSubmit",                       │
│           "additionalContext": "<block>"                             │
│        }}                                                            │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  codex composes the model turn                                       │
│                                                                      │
│   <developer>                                                        │
│     base policy                                                      │
│     (no <skills_instructions> — suppressed by include_instructions)  │
│     ── additionalContext (mega-tron) ──                              │
│     ## Skills (selected for this turn) …                             │
│     ── AGENTS.md (user + repo + admin) ──                            │
│     <skill-used …/> tag contract + mega-tron search guidance         │
│   </developer>                                                       │
│   <user>                                                             │
│     validate this webhook                                            │
│   </user>                                                            │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Model decides                                                       │
│                                                                      │
│   (a) Reference the staged skill by name → codex loads SKILL.md      │
│         body on demand and proceeds                                  │
│                                                                      │
│   (b) Invoke `$webhook-signer` via mention detection (still active   │
│         even with the catalog suppressed) → full SKILL.md body       │
│         auto-injected regardless of catalog visibility               │
│                                                                      │
│   (c) Use `mega-tron search "<task>"` for a skill that wasn't        │
│         pre-routed (typed via the AGENTS.md guidance)                │
│                                                                      │
│   (d) Skip skills → answer from base knowledge                       │
│                                                                      │
│   Model writes the answer.                                           │
│   Per AGENTS.md contract, appends to the final response:             │
│      <skill-used name="webhook-signer" verdict="HELPFUL"             │
│       reason="used HMAC compare helper for the signature check"/>    │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Stop hook fires (turn boundary)                                     │
│  codex executes:                                                     │
│      mega-tron stop-hook                                             │
│  Hook receives on stdin:                                             │
│      {"session_id":"…", "transcript_path":"…",                       │
│       "stop_hook_active": false}                                     │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Inline verdict capture (single-phase, silent)                       │
│                                                                      │
│   • Walk transcript_path; pull every `<skill-used name="…"           │
│     verdict="…" reason="…"/>` tag from the final assistant message  │
│   • Tags without a `verdict=` attribute are skipped (treated as      │
│     no signal; no counter update)                                    │
│   • For each tag, update the skill's SKILL.md frontmatter:           │
│       mega_meta:                                                     │
│         helpful_count: +1   (or harmful_count: +1; NEUTRAL touches   │
│                              last_used_at only)                      │
│         last_used_at: <iso>                                          │
│     and append the reason into helpful_contexts / harmful_contexts   │
│   • Emit empty stdout (no `decision` field) — codex stops cleanly,   │
│     no retry turn, nothing surfaces in the user's terminal           │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Next session's Router.rank() reads the updated mega_meta blocks     │
│  → skills that proved helpful rank higher; harmful ones get demoted  │
│  → adaptive routing without any change to codex itself               │
└──────────────────────────────────────────────────────────────────────┘
```

### Failure modes & their handling

| Symptom                                                | Cause                                                                  | What mega-tron does                                                                                                                  |
|--------------------------------------------------------|------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------|
| `mega-tron: command not found` in codex logs           | `mega-tron` not on the system PATH (only in a project venv)            | `install` resolves the binary via `shutil.which` + interpreter-sibling fallback and stores the **absolute path** in `hooks.json`.    |
| Hook registered but silently never fires               | Codex 2026+ hook trust gate                                            | Installer stamps the `(event_label, command)` SHA-256 into `[hooks.state."…"]` in config.toml at install time. No `/hooks` prompt.   |
| Native catalog still shows up alongside mega-tron's block | `[skills] include_instructions` was not flipped (e.g. fresh install on an old config.toml that the installer couldn't write) | Installer fails loud if it can't write config.toml. Re-run with `--force` after fixing permissions.                                  |
| Wisdom curator never produces new skills               | Daemon disabled (`MEGA_DAEMON=0`) so `wisdom_ignite` no-ops            | Documented degradation. Routing still works against the existing skill pool; only the asynchronous skill-creation path is dormant.   |
| Stop hook surfaces an eval prompt in the user's terminal | Stop handler emitting `{"decision":"block","reason":...}`              | Structurally forbidden: the handler always writes empty stdout. Verdicts come from inline `<skill-used …/>` tags in the same final reply. |

## 2. Claude Code CLI

- **Hook surface.** `UserPromptSubmit` + `Stop` hooks registered in
  `~/.claude/settings.json` with a `_megaOptimusManaged` sentinel
  key on each managed entry (sibling to the documented `matcher` /
  `hooks` keys; Claude Code ignores unknown keys, so the marker is
  invisible to the runtime but lets `--uninstall` strip just our
  entries without disturbing user-owned hooks).
- **Catalog suppression.** Claude Code has no `config.toml`-style
  persistent kill switch. The per-invocation flags that *do* drop
  the catalog —
  [`claude --disallowedTools Skill`](https://code.claude.com/docs/en/cli-reference)
  (removes the `Skill` tool from the model's context entirely) and
  [`claude --bare`](https://code.claude.com/docs/en/cli-reference)
  (skips all hooks/skills/plugins/MCP discovery) — both have to be
  passed on every launch; there is no `settings.json` key that
  permanently disables the `Skill` tool, and `disallowedTools` in
  settings *blocks invocation* but leaves the catalog in the prompt.
  mega-tron therefore offers two strategies that *do* persist
  across sessions and lets the user pick at hook time:
  - **Mode P (passive overlay, default).** Leave the native catalog
    alone. mega-tron emits its top-K block as `additionalContext`
    that *stacks on top of* Claude's flat catalog. Better routing
    signal but no token saving — the full catalog is still loaded.
  - **Mode A (active downgrade), `MEGA_CLAUDE_NATIVE_MODE=active`.**
    Rewrite `~/.claude/settings.local.json`'s `skillOverrides` so
    every non-top-K skill becomes `"name-only"`. The native catalog
    then carries the name but drops the description for those
    skills, recovering most of the token saving the
    `--disallowedTools Skill` flag would have given but persistently
    and per-turn. mega-tron owns the `skillOverrides` key entirely;
    uninstall removes it.
- **Prepend wire format.** `additionalContext` stdout JSON, appended
  to the system prompt for that turn:
  ```json
  {"hookSpecificOutput": {
     "hookEventName": "UserPromptSubmit",
     "additionalContext": "## Skills (selected for this turn …) …"
  }}
  ```
- **CLAUDE.md guidance.** Sentinel-fenced block in
  `~/.claude/CLAUDE.md` (`<!-- >>> mega-tron … >>> -->` … `<!-- <<< mega-tron <<< -->`)
  describes the `<skill-used …/>` tag contract the `Stop` hook
  expects and points the model at `mega-tron search` for skills not
  pre-routed.
- **Self-evaluation loop.** The first-fire `additionalContext` block
  instructs the model to append one inline tag per skill it relied on
  at the end of its final reply:
  `<skill-used name="..." verdict="HELPFUL|HARMFUL|NEUTRAL" reason="..."/>`.
  The `Stop` hook walks `transcript_path` (Claude Code does not
  populate `last_assistant_message` in the Stop payload), pulls every
  such tag from the final assistant message, and writes verdicts into
  each skill's `mega_meta:` frontmatter silently — stdout stays empty,
  no `decision` field, no retry turn. The `reason` attribute must be
  written in English (it is persisted to SKILL.md and the verdict
  embedding store, which is tuned on English).

### End-to-end flow

```
┌──────────────────────────────────────────────────────────────────────┐
│  User starts a session: `claude "validate this webhook"`             │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Claude Code: session bootstrap                                      │
│   • Discover skills under                                            │
│       ~/.claude/skills/, <cwd>/.claude/skills/,                      │
│       any path registered via `mega-tron dirs add`                   │
│   • Load native flat catalog (name + description per skill)          │
│   • If MEGA_CLAUDE_NATIVE_MODE=active and settings.local.json has    │
│       skillOverrides → apply name-only downgrades                    │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  UserPromptSubmit hook fires (first turn of the session)             │
│  Claude Code executes the command registered in                      │
│  ~/.claude/settings.json under hooks.UserPromptSubmit:               │
│      mega-tron claude-hook                                           │
│  Hook receives on stdin:                                             │
│      {"session_id":"…","hook_event_name":"UserPromptSubmit",         │
│       "prompt":"validate this webhook", …}                           │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  mega-tron claude-hook (mega_tron.hosts.claude_code.hook)            │
│                                                                      │
│   1. First-fire check                                                │
│        marker = $XDG_RUNTIME_DIR/mega-tron/seen-claude-<sid>         │
│        if marker exists → emit `{}` and exit (no-op on later turns,  │
│        persistent guidance is carried by CLAUDE.md instead)          │
│                                                                      │
│   2. Daemon-first path (skip if MEGA_DAEMON=0)                       │
│        Try Unix-socket query to the cached router daemon. On hit,    │
│        the daemon already has BGE weights + embedding cache hot      │
│        and returns the top-K block in <50 ms.                        │
│        On miss → spawn the daemon detached and fall through.         │
│                                                                      │
│   3. Cold path: embed + rank in-process                              │
│        BGE embedding (cached at ~/.cache/mega-tron/<slug>.npz)       │
│        Router.rank(prompt, top_k)                                    │
│         = cosine(full SKILL.md, prompt)                              │
│           ⊔ cosine(name-only, prompt)        (max of the two)        │
│           + W_COUNT × β-smoothed helpful/harmful rate                │
│           + W_CONTEXT × (helpful_ctx − W_HARM × harmful_ctx)         │
│           + W_RELATED × (verdict-store HELPFUL − HARMFUL)            │
│         × status_multiplier  {active:1.0, suspect:0.5, archived:-1}  │
│                                                                      │
│   4. Mode A (if MEGA_CLAUDE_NATIVE_MODE=active)                      │
│        non_topk = all_skill_names − {top-K names}                    │
│        rewrite ~/.claude/settings.local.json's skillOverrides so     │
│        every name in non_topk maps to "name-only"                    │
│        atomic write; native catalog descriptions drop for non-top-K │
│                                                                      │
│   5. Render top-K block via build_claude_hook_context()              │
│        emit on stdout:                                               │
│           {"hookSpecificOutput": {                                   │
│              "hookEventName": "UserPromptSubmit",                    │
│              "additionalContext":                                    │
│                "## Skills (selected for this turn by mega-tron)      │
│                 Strongly prefer using one of these skills:           │
│                 /webhook-signer, /timestamp-guard.                   │
│                 - /webhook-signer                                    │
│                     path: /…/.claude/skills/webhook-signer/SKILL.md  │
│                     desc: Validate HMAC-SHA256 webhook signatures.   │
│                 - …                                                  │
│                 ### Self-evaluation                                  │
│                 Append `<skill-used name=… reason=…/>` …"            │
│           }}                                                         │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Claude Code composes the model turn                                 │
│                                                                      │
│   <system>                                                           │
│     base policy                                                      │
│     ── available skills (native catalog) ──                          │
│     - webhook-signer: Validate HMAC-SHA256 …                         │
│     - jwt-verifier: Verify JWT…       ◀── Mode A may have made       │
│     - csrf-token-issuer (name only)        these "name only"         │
│     - … (N entries)                                                  │
│     ── CLAUDE.md ── (carried every turn by Claude Code)              │
│     ## Skill routing (mega-tron)                                     │
│     Strongly prefer skills the hook surfaced. For ad-hoc lookups,    │
│     call `mega-tron search "<task>"`. Emit `<skill-used …/>` …       │
│     ── additionalContext (mega-tron, first turn only) ──             │
│     ## Skills (selected for this turn) …  ◀── Mode P overlay         │
│   </system>                                                          │
│   <user>                                                             │
│     validate this webhook                                            │
│   </user>                                                            │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Model decides                                                       │
│                                                                      │
│   (a) Invoke `/webhook-signer` (slash-triggered Skill call)          │
│         → Claude Code loads SKILL.md body + scripts/ into context    │
│                                                                      │
│   (b) Auto-load on description match                                 │
│         → Same body load, no explicit invocation                     │
│                                                                      │
│   (c) Skip skills → answer from base knowledge                       │
│                                                                      │
│   Model writes the answer.                                           │
│   Per CLAUDE.md contract, appends to the final response:             │
│      <skill-used name="webhook-signer" verdict="HELPFUL"             │
│       reason="used hmac.compare_digest as the skill described;      │
│               test_webhook.py::test_hmac_signature now passes"/>     │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Stop hook fires (turn boundary)                                     │
│  Claude Code executes:                                               │
│      mega-tron claude-stop-hook                                      │
│  Hook receives on stdin:                                             │
│      {"session_id":"…", "transcript_path":"…",                       │
│       "hook_event_name":"Stop", "stop_hook_active": false}           │
│  Note: Claude Code does NOT populate `last_assistant_message` in    │
│  the Stop payload, so the hook tails the transcript instead.         │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Inline verdict capture (single-phase, silent)                       │
│                                                                      │
│   • tracker.scan_transcript() walks transcript_path.jsonl:           │
│       - <skill-used name="…" verdict="…" reason="…"/> tags in        │
│         the final assistant message                                  │
│       - Bash tool_use blocks touching                                │
│         <skills_root>/<name>/scripts/                                │
│   • Tags without a `verdict=` attribute are skipped (treated as      │
│     no signal; no counter update)                                    │
│   • Reason-quality gate: drop verdicts with reasons < ~15 words      │
│     or matching the rejected-phrase list ("ok", "good", "fix", …)    │
│   • For each surviving verdict, verdicts.writer.persist_verdicts:    │
│       mega_meta:                                                     │
│         helpful_count: +1   (or harmful_count: +1; NEUTRAL touches   │
│                              last_used_at only)                      │
│         helpful_contexts: append reason                              │
│         status: active ↔ suspect ↔ archived per ratio + window       │
│         last_session_id, last_updated                                │
│   • Emit empty stdout (no `decision` field) — Claude stops cleanly,  │
│     no retry turn, nothing surfaces in the user's terminal           │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Next session's Router.rank() reads the updated mega_meta blocks     │
│  → helpful skills get count_bonus + context_match nudges             │
│  → harmful skills get harm_match penalty + status downgrade          │
│  → adaptive routing without any change to Claude Code itself         │
└──────────────────────────────────────────────────────────────────────┘
```

### Failure modes & their handling

| Symptom                                                       | Cause                                                                                 | What mega-tron does                                                                                                                                  |
|---------------------------------------------------------------|---------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------|
| `mega-tron: command not found` in Claude Code's hook stderr   | `mega-tron` only in a project venv, not on the user's PATH                            | `install --target claude` resolves the binary via `shutil.which` + interpreter-sibling fallback and stamps the **absolute path** into `settings.json`. |
| Native catalog still ships full descriptions for every skill  | Default mode (P) is overlay-only — by design — and the user expected token savings    | Opt into Mode A: `export MEGA_CLAUDE_NATIVE_MODE=active`. The next hook fire rewrites `settings.local.json`'s `skillOverrides`.                       |
| Stop hook logs "no verdicts captured"                         | Model emitted the prose without the `<skill-used …/>` tag, or omitted `verdict=`      | Silence is treated as no signal — no counter update, better than a noisy one. The CLAUDE.md contract reminds the model on every turn; only emit a tag when there is concrete evidence (file path, test name, command output) to cite. |
| Two parallel hook blocks (mega-optimus + mega-tron) fire each turn | A legacy `mega-optimus install` was never undone before installing mega-tron 2.x | `install` refuses up front and prints the exact `mega-optimus install --uninstall` + `pip uninstall mega-optimus` remediation. `--force` bypasses for advanced users. |
| Stop hook surfaces an eval prompt in the user's terminal      | Stop handler emitting `{"decision":"block","reason":...}`                             | Structurally forbidden: the handler always writes empty stdout. Verdicts come from inline `<skill-used …/>` tags in the same final reply.            |
| First-fire injection didn't happen on a session resume        | Marker file `$XDG_RUNTIME_DIR/mega-tron/seen-claude-<sid>` already exists             | Working as intended — persistent guidance lives in CLAUDE.md, so subsequent turns get the routing rules through Claude Code's memory rendering.       |

## 3. Gemini CLI

- **Hook surface.** `BeforeAgent` + `AfterAgent` hooks registered in
  `~/.gemini/settings.json` with a `_megaOptimusManaged` sentinel
  marker. User-scope, so workspace trust gates don't apply — hooks
  fire regardless of where the user runs `gemini` from.
- **Catalog suppression.** Gemini has **no catalog cap and no
  `include_instructions`-style kill switch.** The only documented
  size lever is `skills.disabled` (per-name blocklist), so mega-tron
  rewrites that array on every first turn to contain every non-top-K
  skill name — this is **Mode-A**. The native catalog then shrinks
  from N skills to K. On the first install mega-tron snapshots the
  user's original `skills.disabled` into
  `settings.json.mega-tron-backup` so uninstall can restore it.
- **Prepend wire format.** Gemini's `BeforeAgent` envelope:
  ```json
  {"hookSpecificOutput": {
     "hookEventName": "BeforeAgent",
     "additionalContext": "## Skills (selected for this turn) ..."
  }}
  ```
- **GEMINI.md guidance.** Sentinel-fenced block in
  `~/.gemini/GEMINI.md` tells the model to prefer `activate_skill` on
  the surfaced names and to emit a `<skill-used …/>` tag per skill
  used.
- **Self-evaluation loop.** Single-phase silent capture, identical to
  Codex/Claude. The model emits `<skill-used name="..." verdict="..."
  reason="..."/>` inline in its final reply (the contract is
  prepended via `build_gemini_hook_context`). On `AfterAgent` the
  hook scans `transcript_path`, pulls those tags, persists verdicts
  via `verdicts.writer.persist_verdicts`, and emits empty stdout —
  no `decision:"deny"`, no retry turn, no loop-guard markers.

### End-to-end flow

```
┌──────────────────────────────────────────────────────────────────────┐
│  User starts a session: `gemini -p "validate this webhook"`          │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Gemini CLI: session bootstrap                                       │
│   • Discover skills under                                            │
│       ~/.gemini/skills/, ~/.agents/skills/,                          │
│       <cwd>/.gemini/skills/, <cwd>/.agents/skills/                   │
│   • Apply skills.disabled blocklist (← Mode-A's surface)             │
│   • Inject every enabled name+description into the system prompt     │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  BeforeAgent hook fires (first turn of the session)                  │
│  Gemini executes the command registered in ~/.gemini/settings.json:  │
│      mega-tron gemini-hook                                           │
│  Hook receives on stdin:                                             │
│      {"session_id":"…","hook_event_name":"BeforeAgent",              │
│       "prompt":"validate this webhook", …}                           │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  mega-tron gemini-hook (mega_tron.hosts.gemini_cli.hook)             │
│                                                                      │
│   1. First-fire check                                                │
│        marker = $XDG_RUNTIME_DIR/mega-tron/seen-gemini-<sid>         │
│        if marker exists → emit `{}` and exit (no-op on later turns)  │
│                                                                      │
│   2. Embed the user prompt with BGE (cached at ~/.cache/mega-tron/)  │
│                                                                      │
│   3. Router.rank(prompt, top_k=5)                                    │
│        cosine similarity × mega_meta ROI weight                      │
│        → ranked list of skills                                       │
│                                                                      │
│   4. Mode-A (catalog shrink, default)                                │
│        non_topk = all_skill_names − {top-K names} ∪ user_pinned      │
│        rewrite ~/.gemini/settings.json's skills.disabled = non_topk  │
│        atomic write (tempfile + rename); native catalog now ≤K       │
│                                                                      │
│   5. Mode-P (additionalContext overlay)                              │
│        render top-K block:                                           │
│           "## Skills (selected for this turn by mega-tron)           │
│            Strongly prefer activating one of these via               │
│            `activate_skill`: webhook-signer, …                       │
│            - webhook-signer                                          │
│                path: /…/.agents/skills/webhook-signer/SKILL.md       │
│                desc: Validate HMAC-SHA256 webhook signatures.        │
│            - …"                                                      │
│        emit on stdout:                                               │
│           {"hookSpecificOutput": {                                   │
│              "hookEventName": "BeforeAgent",                         │
│              "additionalContext": "<block>"                          │
│           }}                                                         │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Gemini composes the model turn                                      │
│                                                                      │
│   <system>                                                           │
│     base policy                                                      │
│     ── available skills ── (now only top-K after Mode-A)             │
│     - webhook-signer: Validate HMAC-SHA256 …                         │
│     - …                                                              │
│     ── additionalContext (mega-tron) ──   ◀── Mode-P overlay         │
│     ## Skills (selected for this turn) …                             │
│     GEMINI.md guidance                                               │
│   </system>                                                          │
│   <user>                                                             │
│     validate this webhook                                            │
│   </user>                                                            │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Model decides                                                       │
│                                                                      │
│   (a) Invoke `activate_skill(name="webhook-signer")`                 │
│         → Gemini loads SKILL.md body into context                    │
│                                                                      │
│   (b) Invoke `read_file` on the SKILL.md path                        │
│         → empirically more common — body lands the same way          │
│                                                                      │
│   (c) Skip skills → answer from base knowledge                       │
│                                                                      │
│   Model writes the answer.                                           │
│   Per GEMINI.md contract, appends to the final response:             │
│      <skill-used name="webhook-signer" verdict="HELPFUL"             │
│       reason="used hmac.compare_digest as the skill described;       │
│               tests/webhooks.py passes"/>                            │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  AfterAgent hook fires (turn boundary)                               │
│  Gemini executes:                                                    │
│      mega-tron gemini-stop-hook                                      │
│  Hook receives on stdin:                                             │
│      {"session_id":"…", "transcript_path":"…",                       │
│       "hook_event_name":"AfterAgent", "stop_hook_active": false}     │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Inline verdict capture (single-phase, silent)                       │
│                                                                      │
│   • tracker.scan_transcript() walks transcript_path.jsonl:           │
│       - <skill-used name="…" verdict="…" reason="…"/> tags in        │
│         the final assistant message                                  │
│       - exec_command tool calls touching                             │
│         <skills_root>/<name>/scripts/                                │
│   • Tags without a `verdict=` attribute are skipped (no signal)      │
│   • ``claimed_use`` invocations (tag without operational trace) are  │
│     rejected to keep discussion-only mentions from inflating         │
│     counters                                                         │
│   • For each surviving verdict, verdicts.writer.persist_verdicts:    │
│       mega_meta:                                                     │
│         helpful_count: +1   (or harmful_count: +1; NEUTRAL touches   │
│                              last_used_at only)                      │
│         helpful_contexts: append reason                              │
│         status: active ↔ suspect ↔ archived per ratio + window       │
│         last_session_id, last_updated                                │
│   • Emit empty stdout — Gemini stops cleanly, no retry turn,         │
│     nothing surfaces in the user's terminal                          │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Next session's Router.rank() reads the updated mega_meta blocks     │
│  → skills that proved helpful rank higher; harmful ones get demoted  │
│  → adaptive routing without any change to Gemini CLI itself          │
└──────────────────────────────────────────────────────────────────────┘
```

### Failure modes & their handling

| Symptom                                                   | Cause                                                                  | What mega-tron does                                                                                                                  |
|-----------------------------------------------------------|------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------|
| `mega-tron: command not found` in Gemini logs             | `mega-tron` not on the system PATH (only in a project venv)            | `install` resolves the binary via `shutil.which` + interpreter-sibling fallback and stores the **absolute path** in `settings.json`. |
| Mode-A's `skills.disabled` doesn't take effect within session | Gemini docs mark `skills.disabled` as **"Requires restart: Yes"** | Mode-P (`additionalContext` overlay) is the always-correct path; Mode-A is best-effort. Disable with `MEGA_GEMINI_MODE=passive`.    |
| Hook never fires                                          | Workspace trust gate                                                   | Hooks are registered at **user scope** (`~/.gemini/settings.json`), not workspace scope, so they fire regardless of `--skip-trust`. |
| Stop hook surfaces an eval prompt in the user's terminal  | Stop handler emitting `{"decision":"deny","reason":...}`               | Structurally forbidden: the handler always writes empty stdout. Verdicts come from inline `<skill-used …/>` tags in the same final reply. |