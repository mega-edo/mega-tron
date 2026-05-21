# Skill Routing in Claude Code

How Claude Code's CLI surfaces skills to the model — storage layout, the
routing pipeline that runs before every turn, the structural limits that
fall out of the design, and the measured impact on a 500-skill pool.

---

## 1. Skill Storage

A "skill" is a directory containing a `SKILL.md` file with YAML
frontmatter (`name`, `description`, optional `mega_meta:` ROI block) and
a Markdown body holding the prompt-time guidance and optional
`scripts/` siblings the model can invoke via `Bash`.

Claude Code discovers skills from two roots (and merges them by name —
later roots shadow earlier ones):

```
~/.claude/skills/                     # user-global skill root
└── <skill-name>/
    └── SKILL.md                      # frontmatter + body
    └── scripts/                      # optional executables

<project>/.claude/skills/             # project-local skill root
└── ...
```

A third root surfaces *plugin-packaged* skills loaded from
`~/.claude/plugins/<plugin>/skills/`. Plugins register through
`enabledPlugins` in `~/.claude/settings.json`; their skills are merged
into the same catalog.

Frontmatter shape (the only two fields Claude Code reads at routing
time):

```yaml
---
name: webhook-signer
description: |
  USE WHEN: signing outgoing webhooks with an HMAC-SHA256 signature
  header. Handles request canonicalisation, timestamp tolerance, and
  constant-time comparison on the receive side.
---
```

`name` is the catalog key. `description` is the only piece of body
text the model ever sees during routing — the full Markdown body is
loaded *only* when the model decides to invoke the skill.

---

## 2. Routing Pipeline

Claude Code routes skills by stuffing a compact catalog into the
system prompt **before the model ever sees the user's message**. Every
turn pays this cost; nothing is on-demand at routing time.

### 2.1 Token budget

The catalog gets a fixed token budget of **`ctx_window ×
skillListingBudgetFraction`** — defaults to **1 % of the context
window**, ~2 000 tokens on the standard 200 K-token model. The
budget fraction is exposed as `skillListingBudgetFraction` (default
`0.01`); `SLASH_COMMAND_TOOL_CHAR_BUDGET` is the env-var override
that sets a fixed character count instead.

Three structural properties of the algorithm (verified against
[code.claude.com/docs/en/skills "Skill descriptions are cut short"](https://code.claude.com/docs/en/skills)):

1. **Per-entry pre-cap.** Each skill's combined
   `description` + `when_to_use` text is truncated to
   `maxSkillDescriptionChars` (default **1 536** characters) *at
   the source*, before the budget step runs. The setting
   `maxSkillDescriptionChars` reconfigures this cap.
2. **Names are always included.** Every discovered skill emits at
   least a name line. Claude Code never drops a skill entirely
   under budget pressure — only descriptions get evicted.
3. **Descriptions are evicted LRU.** Once names are placed, the
   remaining token budget is filled with descriptions. When the
   budget can't fit them all, "descriptions for the skills you
   invoke least are dropped first." Under the fresh-user
   assumption (every invocation count = 0), ties break
   alphabetically — that's the policy a freshly installed Claude
   Code session actually runs.

The selection signal is *past invocation frequency*, not similarity
to the current turn's prompt — the user's prompt is never an input
to which-descriptions-make-the-cut. Once a skill's description
drops out, the model sees only its bare name.

> Reference implementations:
> - Documentation: [code.claude.com/docs/en/skills](https://code.claude.com/docs/en/skills) — "Skill descriptions are cut short" section.
> - Python reproduction in this repo: [`benchmarks/routing/vanilla_sim.py`](../benchmarks/routing/vanilla_sim.py) — `simulate_claude_catalog` reproduces the per-entry cap → name-emit → LRU-fill-descriptions pipeline.

### 2.2 End-to-end flow

```
┌────────────────────────────────────────────────────────────────────┐
│  User types prompt in terminal / VSCode extension                  │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  claude CLI process startup                                        │
│                                                                    │
│  1. Discover skill roots                                           │
│       ~/.claude/skills/                                            │
│       <project>/.claude/skills/                                    │
│       plugin roots from settings.json                              │
│                                                                    │
│  2. Read every SKILL.md frontmatter                                │
│       (name + description only — body is lazy-loaded)              │
│                                                                    │
│  3. Build catalog                                                  │
│       a. cap each description at maxSkillDescriptionChars (1536)   │
│       b. emit every name line — never dropped                      │
│       c. fill remaining 1% × ctx_window token budget with          │
│          descriptions, LRU order (least-invoked dropped first;     │
│          fresh-user → alphabetical tie-break)                      │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  System prompt assembled                                           │
│                                                                    │
│   <system>                                                         │
│     ... base instructions ...                                      │
│     ... Skill tool schema with catalog inline ...   ◀── catalog    │
│     ... CLAUDE.md contents (global + project + local) ...          │
│   </system>                                                        │
│   <user>                                                           │
│     <user prompt>                                                  │
│   </user>                                                          │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  UserPromptSubmit hooks fire (if any registered)                   │
│  e.g. mega-tron injects additionalContext block with top-K      │
│       semantically-ranked skills + their full descriptions         │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  Model decides                                                     │
│                                                                    │
│   (a) Invoke Skill tool ─────► CLI loads <skill>/SKILL.md body     │
│                                  injects body into next turn,      │
│                                  model executes guidance           │
│                                                                    │
│   (b) Invoke Bash tool ──────► touches scripts/* directly          │
│                                                                    │
│   (c) Skip skills ──────────► answers from base knowledge          │
└────────────────────────────────────────────────────────────────────┘
```

### 2.3 Selection signal

Claude Code does **not** rank skills. The catalog is a flat
name-and-description list; the model scans it during its single
forward pass and decides which entry (if any) matches the user's
intent. Selection is the model's pattern match between the prompt and
the description text — there is no embedding lookup, no scoring, no
top-K shortlist baked into the CLI.

This is why `description` quality dominates routing accuracy in a flat
catalog. A skill whose description was dropped from the catalog
(stress condition, see §4) is effectively invisible — only its bare
name remains, and names alone rarely carry enough signal for the
model to pick correctly.

### 2.4 Self-evaluation loop (optional, via hooks)

Claude Code itself has no learning loop — every session starts with
the same catalog and no memory of which skills worked. External hooks
can supply this. Example (mega-tron):

- **`UserPromptSubmit`** hook embeds the prompt, ranks all skills by
  cosine similarity against pre-cached embeddings, and writes the
  top-K full descriptions into the turn's `additionalContext`.
- **`Stop`** hook scans the transcript for `<skill-used>` self-report
  tags and `Bash` calls touching `<skills_root>/<name>/scripts/`, then
  asks the model for a one-turn helpful/harmful verdict and writes the
  result into each skill's `mega_meta:` frontmatter block. Subsequent
  routing rounds factor that ROI into the rank.

The CLI doesn't know any of this happened. It re-reads frontmatter
each turn; the updated `mega_meta:` block influences the *external*
ranker, not Claude Code's flat catalog.

---

## 3. Limits

These fall out of the design above and cannot be configured away from
inside Claude Code:

1. **Catalog cap is soft but the selection algorithm is fixed.** The
   `1 % × context_window ≈ 2 000`-token budget is user-tunable
   (`skillListingBudgetFraction`, `SLASH_COMMAND_TOOL_CHAR_BUDGET`),
   but the *which-descriptions-to-keep* algorithm — least-invoked
   first — is not. The point at which descriptions start dropping
   depends on description verbosity; in the benchmark fixture at
   §4 (500 skills, mean description ≈ 265 chars / ~60 tokens) the
   budget runs out at roughly **~30 surviving descriptions** out of
   500, with the rest reduced to bare names. Once a description
   drops, a name like `password-hash-argon2` is barely
   distinguishable from `password-hash-bcrypt`, and selection
   accuracy collapses on close neighbours.

2. **Names are always included, even when useless.** A 500-skill pool
   pushes ~3 400 tokens of bare names into every turn regardless of
   relevance — more than the 2 000-token budget itself, since the
   "names always emitted" guarantee overrides the budget. This is
   overhead the user pays per turn for skills that will never be
   invoked, and grows linearly with pool size with no upper bound.

3. **No ranking, no retrieval, no scoring.** The CLI treats the
   catalog as a static list. There is no semantic match, no
   prompt-aware shortlist, no "did this skill help last time" signal
   inside Claude Code. Routing quality is bounded by how well the
   model can pattern-match flat text under the cap.

4. **No learning.** Each session is independent. Whether the model
   picked the right skill yesterday has no bearing on today's catalog
   ordering or selection. ROI-aware behaviour requires an external
   hook layer to feed signals back in.

5. **The catalog rides on the `Skill` tool's schema.** Three flags
   on the CLI command line *can* drop the catalog from the system
   prompt: `--tools "<allow-list-without-Skill>"` (an allow-list that
   restricts which built-in tools the model can use), `--disallowedTools
   Skill` (a bare tool name in the deny list *removes* the tool from
   the model's context entirely — see the [CLI reference](https://code.claude.com/docs/en/cli-reference)),
   or `--bare` (skips all skill/hook/plugin/MCP discovery). All
   three are per-invocation flags with no `settings.json` equivalent
   that persists. The `disallowedTools` *setting* in `settings.json`
   only blocks invocation but leaves the catalog itself in the
   prompt — only the bare-tool-name form on the *command line*
   removes it from context. A router that wants to *replace* the
   catalog (not just augment it) must launch the CLI with one of
   these flags every time, with no
   persistent equivalent.

6. **Hook injections stack, they don't replace.** `UserPromptSubmit`
   hooks fire *after* the system prompt has been assembled. Anything
   they inject is additional — it cannot subtract from the catalog
   the CLI already built. Without one of the per-invocation flags in
   #5 (`--tools` / `--disallowedTools Skill` / `--bare`), an
   external router's payload sits on top of the native catalog, not
   in place of it.

---

## 4. Test Results

Full report and raw artefacts at [`benchmarks/hosts/claude/`](../benchmarks/hosts/claude/).

### 4.1 Experimental data

The benchmark draws on three frozen data sources, all version-pinned
and reproducible from the repo:

| Component                    | Origin                                                                                         | Size       |
|------------------------------|------------------------------------------------------------------------------------------------|-----------:|
| **Fixture skills**           | Hand-authored at [`tests/fixtures/skills/`](../tests/fixtures/skills/). Each has a `USE WHEN: …` description and a stub body. | 50 skills  |
| **Prompts**                  | Hand-authored at [`fixtures.yaml`](../benchmarks/hosts/claude/fixtures.yaml). 20 in-distribution (paraphrases of fixture-skill *intent*, written without reading the SKILL.md descriptions) + 5 null prompts that should match nothing. | 25 prompts |
| **Noise pool (`V_stress`)**  | Random sample (seed=42) drawn from the **SkillRet** dataset — HuggingFace [`ThakiCloud/SKILLRET`](https://huggingface.co/datasets/ThakiCloud/SKILLRET), test split. The published SkillRet test split contains 6,660 skills / 4,997 queries / 8,347 qrels; we use 450 of those skills as named-but-irrelevant distractors. Materialized via [`bench/skillret_loader.py`](../bench/skillret_loader.py). | 450 skills |

The fixture skills, prompts, and noise-skill sample are frozen before
any benchmark run. The same three sources back the Codex, Gemini, and
Hermes benchmarks so the cross-host numbers are directly comparable.

### 4.2 Experimental setup

| Field        | Value                                                                              |
|--------------|------------------------------------------------------------------------------------|
| Host CLI     | `claude-code` 2.1.143                                                              |
| Model        | `claude-sonnet-4-6`                                                                |
| Skill pool   | 50 fixture skills (`tests/fixtures/skills/`) ± 450 SkillRet noise skills           |
| Sandbox root | `/tmp/msr-pov-claude/<condition>/` — per-condition `~/.claude/` profile           |
| Fixture      | [`fixtures.yaml`](../benchmarks/hosts/claude/fixtures.yaml) — 20 in-distribution + 5 null prompts |
| Runs         | 1 invocation per `(condition, prompt)` pair → 75 measured cells total              |
| Timeout      | 300 s per cell                                                                     |

### 4.3 Conditions

| Condition     | Pool | Setup |
|---------------|-----:|-------|
| `V_realistic` |   50 | Native catalog only; all 50 fixture skills fit in budget.                                                                                          |
| `V_stress`    |  500 | Native catalog only; 50 fixture skills + 450 SkillRet noise skills. Catalog overflows the 1% × context budget.                                     |
| `ours`        |  500 | Same 500-skill pool, but `Skill` tool excluded via `--tools` whitelist. Native catalog gone. mega-tron `UserPromptSubmit` hook injects top-K=3 descriptions into `additionalContext`. |

### 4.4 Method

- **Tier 1 (deterministic, no API):** dump the assembled system prompt
  via [`run_tier1.py`](../benchmarks/hosts/claude/run_tier1.py) and
  tokenize the `<skills_instructions>` block. Yields `catalog_tokens`
  per condition.
- **Tier 2 (real API):**
  [`run_tier2.py`](../benchmarks/hosts/claude/run_tier2.py) invokes
  `claude -p <prompt>` against the per-condition sandbox, captures the
  transcript at `~/.claude/projects/<hashed-cwd>/<session-id>.jsonl`,
  and extracts the **`tool_use_skills`** signal — the names of skills
  the model actually invoked via the `Skill` tool or via `Bash` into
  `<skills_root>/<name>/scripts/`. This signal works in *all*
  conditions (including R where no native `Skill` tool exists; the
  Bash-into-scripts path remains observable) and is the objective F1
  signal.
- Confusion: TP = in-distribution prompt picked the expected skill;
  FP = null prompt invoked any skill, or in-distribution prompt
  invoked a different skill; FN = expected skill not invoked. See
  [`analysis.py`](../benchmarks/hosts/claude/analysis.py).

### 4.5 Results

| Condition     | Pool | Catalog tok | TP | FP | FN |     P |     R |    F1 | F1 / 1K tok | tok mult |
|---------------|-----:|------------:|---:|---:|---:|------:|------:|------:|------------:|----------|
| `V_realistic` |   50 |       1,962 | 20 |  0 |  0 | 100.0% | 100.0% | 100.0% |       0.510 |    3.31× |
| `V_stress`    |  500 |       2,572 | 15 |  0 |  5 | 100.0% |  75.0% |  85.7% |       0.333 |    4.34× |
| `ours`        |  500 |         592 | 20 |  0 |  0 | 100.0% | 100.0% | 100.0% |       1.689 |  1× base |

- **At 50 skills**, the native catalog has no problem — every
  description fits, the model picks the right skill every time.
  Cost: ~2K tokens per turn.

- **At 500 skills**, the catalog overflows. The 5 missed prompts in
  `V_stress` correspond to in-distribution expected skills whose
  descriptions were evicted from the 2K-token budget; the model saw
  only their names and couldn't disambiguate against semantically
  adjacent noise. F1 drops from 100% to 85.7% purely because of the
  budget cap.

- **With the catalog disabled and an external router**, the same
  500-skill pool gets perfect F1 at **~23% of the token cost**
  (592 vs 2,572). The router's top-K injection carries the full
  descriptions of the few skills that actually match — far below the
  1% budget, and never names-only.

The `ours` row is only achievable when the CLI is launched with the
`--tools` whitelist excluding `Skill`. In default Claude Code usage
that flag isn't set, so an external router's injection stacks on top
of the native catalog (~592 + ~2,572 ≈ ~3,164 tokens per turn). The
quality benefit — better top-K signal under heavy pools — still
applies, but the token saving does not.
