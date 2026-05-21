# Skill Routing in OpenAI Codex CLI

How OpenAI Codex CLI (`codex`) surfaces skills to the model — storage
layout, the catalog-injection pipeline that runs at every turn, the
structural limits the design imposes, and the measured impact on a
500-skill pool against a frozen 25-prompt fixture.

The peer document [Skill Routing Claude](./Skill%20Routing%20Claude.md)
covers the structurally similar Claude Code system; the two differ in
their token budget, ordering policy, mention-detection layer, and
configurability.

---

## 1. Skill Storage

A "skill" is a directory containing a `SKILL.md` file whose YAML
frontmatter declares `name` and `description`, with the Markdown body
holding the prompt-time guidance and optional sibling `scripts/` the
model can invoke via shell.

Codex discovers skills from four scopes, merged by name (later scopes
shadow earlier ones):

```
<cwd>/.agents/skills/                 # REPO    — walked up to repo root
~/.agents/skills/                     # USER    — cross-project personal
/etc/codex/skills/                    # ADMIN   — machine-wide
<bundled with codex>                  # SYSTEM  — imagegen, openai-docs,
                                                  plugin-creator,
                                                  skill-creator,
                                                  skill-installer
```

A second auto-discovery root, `~/.codex/skills/`, used to be the
canonical USER location and is still honoured for backward
compatibility; `~/.agents/` is now the documented default.

Frontmatter shape Codex reads at catalog-build time:

```yaml
---
name: webhook-signer
description: |
  USE WHEN: signing outgoing webhooks with an HMAC-SHA256 signature
  header. Handles request canonicalisation, timestamp tolerance, and
  constant-time comparison on the receive side.
---
```

The full Markdown body is **lazy-loaded** — only when the model
decides to invoke the skill does Codex read and inject the body into
the next turn.

`~/.codex/config.toml` exposes two relevant keys:
`[skills] include_instructions` (boolean, default `true`; set `false`
to suppress the entire `<skills_instructions>` block) and
`[[skills.config]] path = "..." enabled = false` (per-skill disable).
`include_instructions = false` is the **only documented mechanism** to
disable the native catalog wholesale and is the key seam
`mega-tron install` flips so its external router can own routing
end-to-end (see §3.6).

---

## 2. Routing Pipeline

Codex routes skills by inlining a `<skills_instructions>` XML block
into the **developer-role system prompt** at every turn. The block
lists every discovered skill (or as many as fit) with name +
description + absolute file path. Mention-detection runs in parallel
and is independent of the catalog cap.

### 2.1 Token / character budget

The catalog gets a budget of **min(2% of the context window, 8,000
characters)** ([openai/codex#19679], [openai/codex#19911]). When the
context window is unknown Codex falls back to the 8,000-char literal
(constants `DEFAULT_SKILL_METADATA_CHAR_BUDGET: usize = 8_000` and
`SKILL_METADATA_CONTEXT_WINDOW_PERCENT: usize = 2` in
`codex-rs/core-skills/src/render.rs`). Both constants are
**hardcoded** — neither `config.toml` nor any environment variable
exposes an override as of issue #19679 (open). Issue #19911 (open)
requests a UI-driven skill picker because the current CLI
"loads too many skill descriptions, exceeding context limits and
causing truncation"; the existence of that open request is itself
evidence that no prompt-aware ranking layer exists today.

#### Overflow algorithm — verified against `render.rs`

> Reference implementations:
> - Upstream Rust: [`openai/codex` `codex-rs/core-skills/src/render.rs`](https://github.com/openai/codex/blob/main/codex-rs/core-skills/src/render.rs) — functions `build_available_skills`, `render_lines_with_description_budget`, `render_minimum_skill_lines_until_budget`, `build_aliased_available_skills`, `aliased_render_is_better`.
> - Python reproduction in this repo: [`benchmarks/routing/vanilla_sim.py`](../benchmarks/routing/vanilla_sim.py) — `simulate_codex_catalog` reproduces the two-phase budget logic byte-for-byte against the same wire format.


The "minimum cost" of one skill is the length of
`- {name}: (file: {path})` (a `SkillLine::render_minimum()` call —
the `(file: path)` segment is **always present**, even at zero
description chars). The render pipeline branches on whether the
sum of minimum-cost lines fits the budget:

1. **`minimum_cost ≤ budget`** (the common case for small pools).
   Every skill gets its minimum line, then the remaining budget is
   distributed **one character at a time across all skills'
   descriptions in round-robin order**
   (`render_lines_with_description_budget`). Short descriptions
   finish early and their unused share flows to longer descriptions.
   Skills whose description survives intact are "predicted";
   skills whose description gets partially truncated still keep
   their name and path in the catalog.

2. **`minimum_cost > budget`** (large pools — the failure mode that
   dominates `V_stress` in §4). The render falls back to
   `render_minimum_skill_lines_until_budget`: iterate skills in the
   sort order, append the minimum line to the catalog while there
   is budget left, **omit the rest entirely**. The omitted skills
   are invisible to the model — neither name nor description nor
   path appears. A user-facing warning is logged
   (`"Exceeded skills context budget of 2%. … N additional skills
   were not included"`) but the catalog handed to the model carries
   no in-prompt indication of what was cut.

This is the key Codex–Claude divergence: Claude Code's overflow
path keeps names and drops descriptions; **Codex's overflow path
keeps the path-bearing minimum form and drops whole skills**.
A 4 000-char budget (200K context, 2%) admits roughly **55
minimum-form skills**. Past that point each new skill installed
forces an alphabetically-later one out of the catalog entirely.

#### Aliased rendering fallback

When minimum-form rendering would still cause omissions or
description truncation, Codex tries a second pass with **aliased
paths** (`build_aliased_available_skills`). Each absolute path
like `/Users/.../codex/skills/foo/SKILL.md` is replaced with a
short alias like `r0/foo/SKILL.md`. `aliased_render_is_better`
picks the aliased version only if it admits strictly more skills,
or fewer truncated chars, or lower total cost. This buys budget
for users with deeply nested skill installs; it does not change
the structural drop-on-overflow behaviour, only the threshold.

#### Wire format

The line that actually reaches the model:

```
- imagegen: …description… (file: r0/.system/imagegen/SKILL.md)
- openai-docs: …truncated description… (file: r0/.system/openai-docs/SKILL.md)
- a-skill-that-survived-to-minimum-form: (file: r0/a-skill-that-…/SKILL.md)
```

The `(file: path)` suffix is mandatory in every emitted line —
there is no shorter "name only" form. This is what makes Codex's
budget exhaust so fast: every skill line costs at minimum
~60 – 100 chars before the description even starts.

### 2.2 End-to-end flow

```
┌────────────────────────────────────────────────────────────────────┐
│  User issues prompt — `codex exec` or interactive REPL             │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  codex turn startup                                                │
│                                                                    │
│  1. Discover skills across 4 scopes                                │
│        REPO  → walk cwd up to git root                             │
│        USER  → ~/.agents/skills/  (and ~/.codex/skills/ legacy)    │
│        ADMIN → /etc/codex/skills/                                  │
│        SYSTEM → bundled                                            │
│     Merge by name; later scopes shadow earlier.                    │
│                                                                    │
│  2. Read every SKILL.md frontmatter (name + description only).     │
│                                                                    │
│  3. Build <skills_instructions> block                              │
│        budget = min(2% × ctx_window, 8000 chars)                   │
│        order  = SYSTEM-bundled first, then alphabetical by name    │
│        if min-cost lines fit budget:                               │
│          → distribute desc chars round-robin across all skills     │
│        else (large pool):                                          │
│          → emit min-cost lines alphabetically until budget runs    │
│            out; OMIT the rest entirely (no name, no path)          │
│        aliased-path retry if it admits more skills                 │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  Developer-role system prompt assembled                            │
│                                                                    │
│   <developer>                                                      │
│     ... base policy ...                                            │
│     <skills_instructions>                                          │
│       ## Skills                                                    │
│       ### Available skills                                         │
│       - <name>: <description (possibly truncated)>                 │
│         (file: <abs/path/SKILL.md>)                                │
│       - ...                                                        │
│     </skills_instructions>                                         │
│     ... AGENTS.md (user + repo + admin) ...                        │
│   </developer>                                                     │
│   <user>                                                           │
│     <user prompt>                                                  │
│   </user>                                                          │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  UserPromptSubmit hook fires (if registered AND `Trusted` under    │
│  [hooks.state.<key>] — Untrusted entries silently never fire).     │
│  e.g. mega-tron emits additionalContext via stdout JSON with    │
│       top-K semantically-ranked skills + must-use rule.            │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  Two-track selection                                               │
│                                                                    │
│  (a) Mention detection — codex scans the user prompt for           │
│        `$<skill-name>` references; on match the full SKILL.md body │
│        is auto-injected regardless of catalog visibility. Gated    │
│        independently of the cap; works even when                   │
│        `include_instructions = false`.                             │
│                                                                    │
│  (b) Implicit selection — the model reads <skills_instructions>    │
│        and decides whether to invoke a skill by referencing its    │
│        name; on invocation codex loads the body and proceeds.      │
└────────────────────────────────────────────────────────────────────┘
```

### 2.3 Catalog ordering — empirically observed

The documentation does not specify catalog ordering. The frozen
fixtures used in §4 reveal the *de facto* order. Excerpt from
`V_realistic` (55 skills surfaced, 50 fixtures + 5 SYSTEM bundled):

```
- imagegen: ...
- openai-docs: ...
- plugin-creator: ...
- skill-creator: ...
- skill-installer: ...
- api-route-handler: USE WHEN: ...   ← first user/fixture skill
- ...
- webhook-signer: USE WHEN: ...      ← last
```

SYSTEM-bundled skills are pinned at the head. Beyond that the order
is alphabetical by skill name (verified against
[`tier1/raw/V_realistic_*.json`](../benchmarks/hosts/codex/tier1/raw/)).
The order has nothing to do with the user's current prompt — the
model must do all the disambiguation work by scanning description
text.

### 2.4 Mention detection — the only "smart" routing

The mention layer (`$<skill-name>` substring match against discovered
skill names) is the only piece of Codex's routing that actually reads
the user prompt before deciding what to surface. When it fires it
**bypasses the catalog cap entirely** — the matched SKILL.md's full
body is loaded for that turn, even if the same skill's description
was truncated in `<skills_instructions>` or omitted.

It only matches on the literal name token though, so a user prompt
that paraphrases the task in natural language (the common case)
doesn't trigger it. Implicit selection by the model — gated by what
made it past the truncator — is what runs day-to-day.

---

## 3. Limits

These fall out of the design above:

1. **Catalog cap is hard and not user-configurable.**
   `min(2% of ctx_window, 8,000 chars)` is hardcoded
   ([openai/codex#19679], open) — neither config.toml nor any env
   var exposes an override. At a typical 200K-token window the
   8,000-char floor (~2,000 tokens) is binding. The mandatory
   `(file: path)` suffix means each skill costs **~60 – 100 chars
   even before the description**, so the realistic ceiling is
   roughly **~55 skills in minimum form** at 200K context, or
   **30 – 50 skills with full descriptions**. Past that point new
   installs force alphabetically-later skills out of the catalog
   entirely. The only escape valves are patching+rebuilding Codex,
   disabling `include_instructions`, or per-skill disabling via
   `[[skills.config]] enabled = false`.

2. **Overflow drops skills, not just descriptions.** Codex has two
   overflow paths. (a) If minimum-form lines (`- name: (file: path)`)
   still fit the budget, the remaining space is split character-by-
   character across descriptions in round-robin — every skill keeps
   its name and path, only descriptions get shaved. (b) If minimum
   lines themselves overflow, Codex packs them alphabetically until
   the budget runs out and **omits the rest entirely** (no name, no
   path, no description). This is the structural failure mode at
   pool ≳ 55. The warning surfaced to the user reports the omission
   count, but the *model* gets a catalog with no indication of what
   was cut. Unlike Claude Code — which keeps every name in the
   catalog under budget pressure — Codex's path-bearing minimum form
   makes the "all names always emitted" guarantee infeasible at
   scale.

3. **No ranking against the user's prompt.** The catalog is built
   *before* Codex sees what the user asked. Order is determined by
   scope and load sequence, not by relevance to the turn. Even when
   the answer skill's description survives truncation, it sits at
   whatever rank discovery happened to place it at. This is
   structurally the same flat-catalog problem Claude Code has — see
   [Skill Routing Claude § 2.3](./Skill%20Routing%20Claude.md).

4. **No learning loop.** Codex itself has no per-skill ROI
   bookkeeping. Whether a skill helped or hurt yesterday does not
   influence today's catalog order or truncation policy. Hooks
   (UserPromptSubmit + Stop) plus a sidecar ranker can supply this
   externally; the CLI does not.

5. **Hook trust gate (Codex 2026+).** Codex 2026 gates user hooks
   behind an explicit trust check stored under
   `[hooks.state."<key>"]` in `config.toml`. Hooks registered in
   `hooks.json` that do not have a matching `Trusted` entry
   **silently never fire**. Injecting hooks programmatically (e.g.
   an installer) requires stamping a trust hash at install time, or
   the user has to enter `/hooks` interactively.

6. **`include_instructions = false` is the clean kill switch.**
   Unlike Claude Code (where the equivalent requires a
   per-invocation `--tools` flag), Codex has a real, persistent
   `config.toml` key that suppresses the entire
   `<skills_instructions>` block. An external router can therefore
   *replace* (not just augment) Codex's native catalog with zero
   token overlap. The mention-detection layer is gated on the loaded
   skill set, not on this key, so `$<skill-name>` still works after
   disabling the catalog. This is the property `mega-tron
   install` exploits — and it is the single biggest UX difference
   between Codex routing and Claude Code routing.

---

## 4. Test Results

Full report and raw artefacts at [`benchmarks/hosts/codex/`](../benchmarks/hosts/codex/).

### 4.1 Experimental data

The benchmark draws on three frozen data sources, all version-pinned
and reproducible from the repo:

| Component                    | Origin                                                                                         | Size       |
|------------------------------|------------------------------------------------------------------------------------------------|-----------:|
| **Fixture skills**           | Hand-authored at [`tests/fixtures/skills/`](../tests/fixtures/skills/). Each has a `USE WHEN: …` description and a stub body. | 50 skills  |
| **Prompts**                  | Hand-authored at [`fixtures.yaml`](../benchmarks/hosts/codex/fixtures.yaml). 20 in-distribution (paraphrases of fixture-skill *intent*, written without reading the SKILL.md descriptions) + 5 null prompts that should match nothing. | 25 prompts |
| **Noise pool (`V_stress`)**  | Random sample (seed=42) drawn from the **SkillRet** dataset — HuggingFace [`ThakiCloud/SKILLRET`](https://huggingface.co/datasets/ThakiCloud/SKILLRET), test split. The published SkillRet test split contains 6,660 skills / 4,997 queries / 8,347 qrels; we use 450 of those skills as named-but-irrelevant distractors. Materialized via [`bench/skillret_loader.py`](../bench/skillret_loader.py). | 450 skills |

The fixture skills, prompts, and noise-skill sample are frozen before
any benchmark run. The same three sources back the Claude, Gemini,
and Hermes benchmarks so the cross-host numbers are directly
comparable.

### 4.2 Experimental setup

| Field        | Value                                                                              |
|--------------|------------------------------------------------------------------------------------|
| Host CLI     | `codex-cli` 0.116.0                                                                |
| Model        | `gpt-5.5` (Codex's default; recorded in every Tier 2 transcript header)            |
| Reasoning    | `model_reasoning_effort = "xhigh"` (from `~/.codex/config.toml`)                   |
| Skill pool   | 50 fixture skills (`tests/fixtures/skills/`) ± 450 SkillRet noise skills           |
| Sandbox root | `/tmp/msr-pov/<condition>/` — per-condition `CODEX_HOME` profile                  |
| Fixture      | [`fixtures.yaml`](../benchmarks/hosts/codex/fixtures.yaml) — 20 in-distribution + 5 null prompts |
| Runs         | 2 invocations per `(condition, prompt)` pair (`run_idx=1, 2`) → 150 measured cells |
| Timeout      | 240 s per cell                                                                     |

### 4.3 Conditions

| Condition     | Pool | Setup |
|---------------|-----:|-------|
| `V_realistic` |   50 | Codex default. Catalog fits comfortably.                                                                                                          |
| `V_stress`    |  500 | Codex default. 50 fixtures + 450 named-noise skills from the SkillRet test split. Catalog cap forces severe truncation.                            |
| `ours`        |  500 | Same pool; `[skills] include_instructions = false` set in `config.toml`. Native catalog suppressed. mega-tron `UserPromptSubmit` hook injects top-K descriptions into `additionalContext`. |

### 4.4 Method

- **Tier 1 (deterministic, no API):**
  [`run_tier1.py`](../benchmarks/hosts/codex/run_tier1.py) invokes
  `codex debug prompt-input <prompt>` per `(condition, prompt)` and
  parses the developer-role messages emitted to the model. Yields
  `catalog_tokens` (total developer-role token cost) and
  `expected_skill_rank` inside the `<skills_instructions>` block.
- **Tier 2 (real API):**
  [`run_tier2.py`](../benchmarks/hosts/codex/run_tier2.py) invokes
  `codex exec --skip-git-repo-check --sandbox <mode> --cd <cwd>
  <prompt>` against the sandbox `CODEX_HOME` and captures the session
  JSONL. F1 signal is **`<skill-used>` tag OR SKILL.md file read OR
  expected-skill path mention** in the assistant transcript —
  cross-checked against `scripts/`-touching shell calls. See
  [`analysis.py`](../benchmarks/hosts/codex/analysis.py).
- Confusion: TP = in-distribution prompt resolved by the expected
  skill via any of the three signals; FP = null prompt invoked any
  skill, or in-distribution prompt resolved by a different skill;
  FN = expected skill not surfaced.

### 4.5 Results

| Condition     | Pool | Catalog tok | TP | FP | FN |     P |     R |    F1 | F1 / 1K tok | tok mult |
|---------------|-----:|------------:|---:|---:|---:|------:|------:|------:|------------:|----------|
| `V_realistic` |   50 |       5,305 | 20 |  0 |  0 | 100.0% | 100.0% | 100.0% |       0.189 |    8.41× |
| `V_stress`    |  500 |       6,910 |  3 |  4 | 17 |  42.9% |  15.0% |  22.2% |       0.032 |   10.95× |
| `ours`        |  500 |         631 | 20 |  0 |  0 | 100.0% | 100.0% | 100.0% |       1.585 |  1× base |

- **At 50 skills**, the catalog holds every description; the model
  has all the signal it needs and picks correctly every time. The
  cost is ~5,300 tokens of inlined catalog on every turn.

- **At 500 skills**, the catalog cap bites hard. The minimum-form
  budget (`- name: (file: path)`, ~75 chars/skill) saturates the
  4 000-char ceiling at roughly **55 skills**; alphabetically-later
  skills are omitted from the catalog entirely — not even their
  name or path reaches the model. F1 collapses to 22.2 % — the
  model misses 17 of 20 in-distribution prompts because the right
  skill was either dropped (most common) or its description was
  shaved to uselessness (when it landed in the surviving 55). 4
  false positives reflect the model grasping at semantically-adjacent
  noise skills whose names happened to land in the surviving alphabetic
  slice.

- **With `include_instructions = false` + external router**, the
  same 500-skill pool gets perfect F1 at **~9% of the native-catalog
  token cost** (631 vs 6,910 tokens). The router injects only the
  top-K relevant skills with full descriptions; no parallel native
  catalog burns tokens.

### 4.6 Comparison with Claude Code

For the same fixture on the same 500-skill pool, Claude Code's
native catalog held F1 at **85.7%** under stress (descriptions
dropped but names retained for all 500 skills), while Codex's
native catalog collapsed to **22.2%** (~55 skills survived in
path-bearing minimum form, ~445 omitted entirely). Two structural
reasons for the gap:

1. **Codex's wire format requires `(file: path)` on every line.**
   The mandatory path suffix means the minimum cost per skill is
   ~60 – 100 chars before any description, so the 4 000-char
   budget caps the catalog at ~55 skills regardless of how short
   descriptions are. Claude Code's wire format is just `- name`
   with optional description, so name-only lines cost ~30 chars
   and all 500 names fit comfortably.
2. **Codex omits skills past the cap.** Once minimum-form lines
   themselves overflow, surplus skills disappear from the block
   entirely — they are invisible to implicit selection (mention
   detection still works if the user types `$name`, but no
   natural-language prompt would). Claude Code keeps all names
   in the catalog regardless of cap pressure; only descriptions
   get dropped.

The `ours` condition saturates at 100% F1 on both products, so the
ceiling story is the same once the catalog is replaced by a semantic
top-K router. The difference between Codex and Claude Code shows up
in the *vanilla* failure mode, not in what mega-tron can recover.
