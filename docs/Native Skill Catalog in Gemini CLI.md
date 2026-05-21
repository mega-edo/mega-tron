# Native Skill Catalog in Gemini CLI

How Google Gemini CLI surfaces skills to the model — storage layout, the
routing pipeline that runs at session start, the structural limits that
fall out of the design, and the measured impact on a 500-skill pool.

The peer documents [Native Skill Catalog in Claude Code](./Native%20Skill%20Catalog%20in%20Claude%20Code.md)
and [Native Skill Catalog in Codex CLI](./Native%20Skill%20Catalog%20in%20Codex%20CLI.md) cover hosts
that flatten their discovered skill pool into the prompt and cap it
(Claude at 1% × context, Codex at 2% / 8K chars). **Gemini does not
cap at all** — every discovered skill's metadata is loaded into the
system prompt regardless of pool size. That is the single most
important property of Gemini's design and the source of its
distinctive failure mode at scale.

---

## 1. Skill Storage

A "skill" is a directory containing a `SKILL.md` file with YAML
frontmatter (`name`, `description`) and a Markdown body holding the
prompt-time guidance and optional `scripts/` and `references/`
siblings the model can read via `read_file`.

Gemini auto-discovers skills from four roots (anything outside is
invisible to native discovery, regardless of `--include-directories`):

```
<cwd>/.gemini/skills/                # workspace-local skill root
└── <skill-name>/
    └── SKILL.md                     # frontmatter + body
    └── scripts/                     # optional, lazy-loaded

<cwd>/.agents/skills/                # workspace alias of above
~/.gemini/skills/                    # user-global skill root
~/.agents/skills/                    # user alias of above
```

Skills are merged by name; on collision the later-scanned scope wins
and `gemini skills list` emits a `Skill conflict detected` line to
stderr. A plain `skills/` directory at the workspace root is **not**
scanned — community guides frequently get this wrong, and we tripped
on it during proof-of-value setup (see §4).

Frontmatter shape (the only two fields Gemini reads at routing time):

```yaml
---
name: webhook-signer
description: Validate incoming webhook requests using HMAC-SHA256 with
             a 5-minute anti-replay window. Use when verifying inbound
             signed callbacks from third-party services.
---
```

`name` is the catalog key. `description` is the only piece of body
text the model ever sees during routing — the full Markdown body is
loaded *only* when the model invokes `activate_skill` or `read_file`s
the SKILL.md path.

`~/.gemini/settings.json` exposes two skill keys: `skills.enabled`
(boolean, default `true`, master kill switch) and `skills.disabled`
(string array, per-skill blocklist by name). Both are documented as
**"Requires restart: Yes"** in the
[configuration reference](https://geminicli.com/docs/reference/configuration/),
meaning toggles do not take effect mid-session. There is no
`include_instructions` equivalent and no character or token budget
key.

---

## 2. Routing Pipeline

Gemini routes skills by inlining every discovered skill's
`name` + `description` pair into the model's system prompt at
**session start**. The block is built once per session, not per turn,
and persists for the lifetime of the session.

### 2.1 No token budget

The catalog gets **no budget at all**. Every enabled skill is
emitted as a structured XML `<skill>` element with three children —
`<name>`, `<description>`, `<location>` — wrapped in an
`<available_skills>` block under an `# Available Agent Skills`
markdown header
(`packages/core/src/prompts/snippets.ts::renderAgentSkills` in
[`google-gemini/gemini-cli`](https://github.com/google-gemini/gemini-cli)).
There is no documented cap and no overflow policy because there is
no overflow path — the catalog grows linearly with pool size, and
the XML structure imposes ~30 tokens of fixed structural overhead
per skill on top of the description text.

Wire format (verbatim from the source):

```text
# Available Agent Skills

You have access to the following specialized skills. To activate a
skill and receive its detailed instructions, call the activate_skill
tool with the skill's name.

<available_skills>
  <skill>
    <name>webhook-signer</name>
    <description>USE WHEN: signing outgoing webhooks…</description>
    <location>.gemini/skills/webhook-signer/SKILL.md</location>
  </skill>
  <skill>
    …
  </skill>
</available_skills>
```

> Reference implementations:
> - Upstream TypeScript: [`google-gemini/gemini-cli` `packages/core/src/prompts/snippets.ts`](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/prompts/snippets.ts) — function `renderAgentSkills`.
> - Python reproduction in this repo: [`benchmarks/routing/vanilla_sim.py`](../benchmarks/routing/vanilla_sim.py) — `simulate_gemini_catalog` emits the XML block byte-for-byte against the same wire format.

Measured against the benchmark fixture (§4) at three pool sizes,
showing how the XML wire format inflates the per-skill cost:

| Pool | Catalog tokens | Tokens / skill |
|---:|---:|---:|
| 59 | 6 005 | ~102 |
| 183 | 17 957 | ~98 |
| 500 | 49 455 | ~99 |

The cost is roughly **~99 tokens / skill** end-to-end (description
text + `<name>` + `<description>` + `<location>` + XML framing).
Extrapolating to a real-user pool of 1 600+ enabled skills puts the
catalog at well over **150 K tokens per session**. At Gemini 3 Pro's
2M-token context window that is ~7.5 % of capacity burned on skill
metadata before the user has typed anything; for a five-turn session
re-sending the system prompt each turn, the marginal input-token
cost runs to **~750 K tokens of metadata alone** (~$2 / session at
the public input-token rate). That is far past the documented
operating sweet spot of 50 – 100 skills cited in the GA announcement.

This is the explicit design choice: the
[Agent Skills GA blog post](https://blog.google/technology/developers/agent-skills-gemini-cli/)
positions the system-prompt injection as "progressive disclosure
stage 1: discovery" — the intent is that *loading every
name+description* is cheap and *loading SKILL.md bodies* is the
expensive step, deferred until activation. The arithmetic works at
50 skills (a few thousand tokens) and fails progressively as the
pool scales.

### 2.2 End-to-end flow

```
┌────────────────────────────────────────────────────────────────────┐
│  User starts session — `gemini -p <prompt>` or interactive REPL    │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  gemini session startup                                            │
│                                                                    │
│  1. Discover skill roots                                           │
│       <cwd>/.gemini/skills/                                        │
│       <cwd>/.agents/skills/   (alias)                              │
│       ~/.gemini/skills/                                            │
│       ~/.agents/skills/       (alias)                              │
│                                                                    │
│  2. Read every SKILL.md frontmatter                                │
│       (name + description only — body is lazy-loaded)              │
│       Apply settings.json skills.disabled blocklist                │
│                                                                    │
│  3. Build catalog                                                  │
│       budget = ∞   ← all N enabled skills loaded, no cap           │
│       wire format = XML <skill><name>…</name>                      │
│                          <description>…</description>              │
│                          <location>path</location></skill>         │
│       order  = filesystem-iteration (not relevance, not            │
│                alphabetical)                                       │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  System prompt assembled                                           │
│                                                                    │
│   <system>                                                         │
│     ... base policy ...                                            │
│     # Available Agent Skills                                       │
│     <available_skills>                                             │
│       <skill>                                                      │
│         <name>name-1</name>                                        │
│         <description>description-1</description>                   │
│         <location>.gemini/skills/name-1/SKILL.md</location>        │
│       </skill>                                                     │
│       ... (ALL N skills as XML, no cap) ...                        │
│     </available_skills>                                            │
│     ... GEMINI.md (user + workspace) ...                           │
│   </system>                                                        │
│   <user>                                                           │
│     <user prompt>                                                  │
│   </user>                                                          │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  BeforeAgent hooks fire (if registered AND workspace trusted)      │
│  e.g. mega-tron emits additionalContext with top-K              │
│       semantically-ranked skills + their full descriptions         │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  Model decides                                                     │
│                                                                    │
│   (a) Invoke `activate_skill` tool ─► runtime loads SKILL.md body  │
│                                                                    │
│   (b) Invoke `read_file` on SKILL.md ─► body enters context        │
│                                         (empirically more common)  │
│                                                                    │
│   (c) Skip skills ────────────────────► answers from base knowledge│
└────────────────────────────────────────────────────────────────────┘
```

### 2.3 Selection signal

Gemini does **not** rank skills. The catalog is a flat
name-and-description list; the model scans it during its single
forward pass and decides which entry (if any) matches the user's
intent. There is no embedding lookup, no scoring, no top-K shortlist
baked into the CLI, and no mention-detection layer
(`$<skill-name>` shorthand exists in neither the docs nor the
binary). The model is the only router.

This is why `description` quality dominates routing accuracy on a
flat catalog — and why the catalog's lack of a cap matters less for
**signal preservation** (every description survives intact) than it
does for **signal-to-noise ratio** (the right description sits among
1,670 distractors).

### 2.4 Self-evaluation loop (optional, via hooks)

Gemini itself has no learning loop — every session starts with the
same catalog and no memory of which skills worked. External hooks can
supply this. Example (mega-tron):

- **`BeforeAgent`** hook embeds the prompt, ranks all skills by
  cosine similarity against pre-cached embeddings, and writes the
  top-K full descriptions into the turn's `additionalContext`.
- **`AfterAgent`** hook captures the model's inline
  `<skill-used name="…" verdict="…" reason="…"/>` tags from the same
  final reply (the verdict contract is prepended via
  `build_gemini_hook_context`), persists them via
  `verdicts.writer.persist_verdicts`, and emits empty stdout — no
  retry turn, identical to Codex/Claude. Subsequent ranking rounds
  factor that ROI into the rank.

The CLI doesn't know any of this happened. It re-reads frontmatter
each session; the updated `mega_meta:` block influences the
*external* ranker, not Gemini's flat catalog.

---

## 3. Limits

These fall out of the design above and cannot be configured away
from inside Gemini:

1. **No catalog cap.** Every discovered skill's metadata is injected
   into the system prompt, every session, wrapped in `<skill>` XML
   tags that add ~30 tokens of structural overhead per entry on top
   of the description text. Measured cost in the §4 fixture:
   ~6 K tokens at pool=59, ~18 K at pool=183, ~49 K at pool=500 —
   a steady ~99 tokens / skill. Extrapolating to a real-user pool
   of 1 600+ enabled skills puts the catalog at well over 150 K
   tokens per session — ~7.5 % of a 2M context, ~$0.45 input cost
   on the first turn, ~$2 for a five-turn session. The cost scales
   linearly with pool size and has no upper bound short of
   `skills.enabled = false`.

2. **No relevance ranking against the user's prompt.** The catalog
   is built before Gemini sees what the user asked. Order is
   filesystem-iteration, not relevance. The right skill — when it
   exists in the pool — sits at whatever rank discovery happened to
   put it at, surrounded by competitors. The model must read every
   description and choose.

3. **No learning, no ROI memory.** Each session is independent.
   Whether the model picked the right skill yesterday has no bearing
   on today's catalog. There is no `mega_meta`-equivalent in the
   native runtime; ROI-aware behaviour requires an external hook
   layer to feed signals back in.

4. **No mention-detection layer.** Codex has `$<skill-name>` and
   Claude Code has `/<skill-name>`; Gemini has neither. The native
   `activate_skill` tool is the only explicit invocation surface and
   the model must decide to call it on its own. There is no
   user-side override short of typing the SKILL.md path into the
   prompt and asking the model to `read_file` it.

5. **`skills.disabled` requires restart.** Both `skills.enabled` and
   `skills.disabled` are marked **"Requires restart: Yes"** in the
   configuration reference. The obvious dynamic-routing strategy —
   rewrite `skills.disabled` to contain every non-top-K name at each
   turn — is officially unsupported as a within-session operation.
   mega-tron's experimental Mode-A attempts it anyway and *does*
   take effect on the next turn in our measurements, but Gemini's
   docs reserve the right to break this.

6. **Hook trust is the silent footgun.** Workspace-level hooks only
   fire if the workspace is trusted. `--skip-trust` grants session
   trust but **does not load workspace settings** — a distinction
   the public hook reference does not document. To get workspace
   hooks to fire, you need `GEMINI_CLI_TRUST_WORKSPACE=true` env
   var, or a `<cwd>/.gemini/trustedFolders.json`, or interactive
   `/trust` acceptance. Without it the router hooks silently never
   fire and there is no warning surfaced anywhere.

7. **`skills.enabled = false` is the kill switch, but blunt.**
   Unlike Codex's `include_instructions = false` (which leaves
   mention detection working), Gemini's `skills.enabled = false`
   kills *all* native skill machinery: `activate_skill` disappears
   from the tool list, `skills.disabled` becomes moot. An external
   router that uses this kill switch effectively replaces Gemini's
   skill system end-to-end. mega-tron instead leaves the native
   catalog on and layers an additionalContext overlay on top — the
   catalog token cost is still paid at session start unless
   Mode-A successfully prunes it.

---

## 4. Test Results

Full report and raw artefacts at [`benchmarks/hosts/gemini/`](../benchmarks/hosts/gemini/).

### 4.1 Experimental data

The benchmark draws on three frozen data sources, all version-pinned
and reproducible from the repo:

| Component                    | Origin                                                                                         | Size       |
|------------------------------|------------------------------------------------------------------------------------------------|-----------:|
| **Fixture skills**           | Hand-authored at [`tests/fixtures/skills/`](../tests/fixtures/skills/). Each has a `USE WHEN: …` description and a stub body. | 50 skills  |
| **Prompts**                  | Hand-authored at [`fixtures.yaml`](../benchmarks/hosts/gemini/fixtures.yaml). 20 in-distribution (paraphrases of fixture-skill *intent*, written without reading the SKILL.md descriptions) + 5 null prompts that should match nothing. | 25 prompts |
| **Noise pool (`V_stress`)**  | Random sample (seed=42) drawn from the **SkillRet** dataset — HuggingFace [`ThakiCloud/SKILLRET`](https://huggingface.co/datasets/ThakiCloud/SKILLRET), test split. The published SkillRet test split contains 6,660 skills / 4,997 queries / 8,347 qrels; we use 450 of those skills as named-but-irrelevant distractors. Materialized via [`bench/skillret_loader.py`](../bench/skillret_loader.py). | 450 skills |

The fixture skills, prompts, and noise-skill sample are frozen before
any benchmark run. The same three sources back the Claude, Codex, and
Hermes benchmarks so the cross-host numbers are directly comparable.

### 4.2 Experimental setup

| Field        | Value                                                                              |
|--------------|------------------------------------------------------------------------------------|
| Host CLI     | `gemini-cli` 0.42.0                                                                |
| Model        | `auto-gemini-3` (the runtime's default; routes across `gemini-3-pro-preview` and `gemini-3-flash-preview`) |
| Skill pool   | 50 fixture skills (`tests/fixtures/skills/`) ± 450 SkillRet noise skills          |
| Sandbox root | `/tmp/povg/<condition>/workspace/.gemini/skills/` (Gemini's documented workspace-level discovery path) |
| Workspace trust | `GEMINI_CLI_TRUST_WORKSPACE=true` at session start — required to load workspace hooks; `--skip-trust` alone is insufficient (§3.6 footgun) |
| Fixture      | [`fixtures.yaml`](../benchmarks/hosts/gemini/fixtures.yaml) — 20 in-distribution + 5 null prompts |
| Runs         | 1 invocation per `(condition, prompt)` pair → 75 measured cells total              |
| Timeout      | 240 s per cell (4 timeout cells re-run at 480 s — see §4.4)                        |

### 4.3 Conditions

| Condition     | Pool | Setup |
|---------------|-----:|-------|
| `V_realistic` |   50 | Gemini default. 50 fixture skills, system-prompt catalog at 3,076 tokens.                                                                            |
| `V_stress`    |  500 | Gemini default. 50 fixtures + 450 SkillRet noise skills. System-prompt catalog grows to 28,140 tokens (no truncation; uncapped).                     |
| `ours`        |  500 | Same 500-skill pool. mega-tron `BeforeAgent` hook emits a top-K=3 semantically-ranked block as `additionalContext`; native catalog remains active. |

### 4.4 Method

- **Catalog-token measurement (deterministic):**
  [`analyze.py:_vanilla_catalog_tokens`](../benchmarks/hosts/gemini/analyze.py)
  tokenizes the YAML frontmatter (`name` + `description`) of every
  enabled SKILL.md in the sandbox skills directory using `tiktoken`
  (`o200k_base`). For the router condition, the `BeforeAgent`
  `additionalContext` block is rendered for a representative prompt
  and tokenized.
- **F1 (real API):**
  [`run_bench.py`](../benchmarks/hosts/gemini/run_bench.py) invokes
  `gemini --output-format stream-json -p <prompt>` against the
  per-condition sandbox and streams each tool_use / tool_result event
  to `transcripts/<condition>__<prompt>.jsonl`. F1 signal is
  **`read_skill_mds`** — the names of skills the model opened via
  `read_file` against `<sandbox>/.gemini/skills/<name>/SKILL.md`. This
  is Gemini's equivalent of Claude's `tool_use_skills` and Codex's
  `<skill-used>` tag; `activate_skill` events are tracked separately
  and reported in §4.5 but not used for F1.
- Confusion: TP = in-distribution prompt read the expected SKILL.md;
  FP = null prompt read any SKILL.md, or in-distribution prompt read
  a different skill; FN = expected SKILL.md not read.

### 4.5 Results

| Condition     | Pool | Catalog tok | TP | FP | FN |      P |      R |     F1 | F1 / 1K tok | tok mult |
|---------------|-----:|------------:|---:|---:|---:|-------:|-------:|-------:|------------:|----------|
| `V_realistic` |   50 |       3,076 | 20 |  0 |  0 | 100.0% | 100.0% | 100.0% |       0.325 |    4.75× |
| `V_stress`    |  500 |      28,140 | 16 |  0 |  4 | 100.0% |  80.0% |  88.9% |       0.032 |   43.43× |
| `ours`        |  500 |         648 | 18 |  0 |  2 | 100.0% |  90.0% |  94.7% |       1.462 |  1× base |

- **At 50 skills**, the catalog holds every description; the model
  picks correctly every time. Cost: ~3,100 tokens of system-prompt
  catalog per session.

- **At 500 skills**, the catalog grows linearly to 28,140 tokens
  with no truncation — every description fully intact. F1 *does not*
  collapse the way Codex's does (Codex drops to 22.2% at this pool
  size); Gemini holds at 88.9%. The four lost prompts are cases
  where the right description sits semantically adjacent to noise
  skills — the model is confused, not starved. The F1/1Ktok
  efficiency, however, collapses from 0.325 to 0.032 — a 10× drop
  in efficiency for the same pool-size jump.

- **With the mega-tron BeforeAgent overlay** on the same
  500-skill pool, F1 recovers from 88.9% to 94.7% at **2.3% of the
  vanilla catalog token cost** (648 vs 28,140 tokens). F1/1Ktok
  jumps to 1.462 — 46× more efficient than `V_stress`, 4.5× more
  efficient than the 50-skill vanilla baseline.

Notes on harness reliability:
- The first 75-cell pass crashed at cell 74/75 before
  `run_bench.py`'s final json dump. 73 cells were recovered from
  per-cell transcripts via
  [`rebuild_results.py`](../benchmarks/hosts/gemini/rebuild_results.py);
  the remaining 2 (R/null_math, R/null_recipe) were re-run individually.
- Two cells hit the original 240 s wall-clock budget
  (`V_realistic/oauth_pkce`, `V_stress/kafka_consume`) and were also
  re-run with `--timeout 480` for a fair comparison; both completed
  in under 65 s on the second pass.

### 4.6 Comparison with Codex and Claude Code

For the same 500-skill fixture and equivalent mega-tron setup:

| Product       | Vanilla F1 at 500 skills | Catalog tok at 500 skills | mega-tron F1 | mega-tron tok |
|---------------|-------------------------:|--------------------------:|----------------:|-----------------:|
| Codex 0.130   |                    22.2% |                     6,910 |          100.0% |              631 |
| Claude Code   |                    85.7% |                     2,572 |          100.0% |              592 |
| Gemini 0.42   |                    88.9% |                    28,140 |           94.7% |              648 |

Three observations:

1. **Gemini's vanilla F1 at 500 skills is the best of the three.**
   The uncapped catalog *does* deliver more signal to the model than
   the truncated ones do — Gemini trades token efficiency for
   accuracy and that trade pays off on F1.
2. **Gemini's vanilla token cost is the worst of the three by a
   wide margin** — 4–14× the others, scaling linearly with pool
   size. At 1,671 enabled skills (the real user pool) the bill rises
   to 137K tokens per session.
3. **mega-tron token costs are essentially identical across all
   three products** (~650 tokens each). The router's job — compress
   the catalog by ~40× without surrendering F1 — is delivered
   consistently regardless of host.

The structural advantage of mega-tron on Gemini is *not* in the
F1 axis (vanilla is already good at moderate pool sizes); it is in
the **F1-per-token efficiency** axis and in the **scaling axis** —
vanilla degradation is linear in pool size, the router's is flat.
For a 50-skill user mega-tron is a marginal accuracy boost; for
the 1,671-skill user, it is the difference between a $2 / 5-turn
session and an $0.05 / 5-turn session, at *higher* F1.
