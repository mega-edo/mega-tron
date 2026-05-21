# mega-tron Routing Benchmark — Results

## 1. Purpose

Quantify how meaningfully mega-tron's prompt-aware top-K router optimises
the skill catalog that host CLIs (Codex / Claude Code / Gemini) inject
into the model context, against the catalog policy each CLI ships with
by default. The benchmark answers two questions:

- **H1 (Quality)**: Does prompt-aware top-K routing recover more of the
  skills a user actually needs (coverage) than static, prompt-independent
  catalog injection?
- **H2 (Efficiency)**: At what token cost does each policy operate, and
  how does that cost scale as the skill pool grows?

The goal is an objective, hermetic, API-free measurement — every result
below is reproducible from the committed pool and query fixture without
calling any external model.

## 2. Experimental Setup

### 2.1 Data

#### Skill pool — 500 third-party SKILL.md files

The benchmark draws from a 500-skill pool deterministically sampled
(`seed=42`) from publicly published, permissively licensed skill packs
authored by independent third parties. None of the skills were written
for this benchmark; they were collected from the same ecosystem any
real Codex / Claude Code / Gemini user would install from.

**Authorship breakdown** (extracted from SKILL.md frontmatter
`author:` field):

| Skills | Author | Upstream |
|---|---|---|
| 449 | Jeremy Longshore | [jeremylongshore/skills](https://github.com/jeremylongshore/skills) — a community-maintained MIT-licensed catalogue covering dev-tools, SDK migrations, MLOps, and SaaS integrations |
| 27 | Orchestra Research | published MIT-licensed pack |
| 4 | Railway | Railway's official skill pack |
| 2 | Claude Code | Anthropic-published Claude Code starter skills |
| 1 | Vercel Engineering | from `vercel-labs/skills` |
| 1 | manim-community | manim animation library skill |
| 1 | Damien Laine | individual contributor |
| 1 | (openai-docs, Apache-2.0) | OpenAI Codex official skill bundle |
| 14 | unspecified | frontmatter omitted author field — all carry SPDX license tags |

**License composition** — 494 MIT, 6 Apache-2.0. Every entry passes the
benchmark's permissive-SPDX gate (`Apache-2.0`, `MIT`, `BSD-2/3-Clause`,
`CC0-1.0`, `CC-BY-4.0`, `Unlicense`). The full audit table lives at
`benchmarks/routing/200bench/LICENSES.md`.

**Sampling procedure** —
1. SHA-256 dedup with first-wins (the same skill installed twice counts
   once).
2. Drop any skill without a permissive SPDX identifier (sources checked
   in order: SKILL.md frontmatter `license:`, sibling `LICENSE` file,
   parent-dir `LICENSE` file).
3. `Random(42).sample(licensed_pool, 500)`.

The 500 SKILL.md files are committed under
`benchmarks/routing/skills/<sha8>/` with their full
`pool_manifest.json` (per-skill `sha256`, `source_root`,
`original_dirname`, `byte_size`, `license_spdx`, `license_source`) for
byte-for-byte reproducibility on a clean checkout.

#### Gold skills — 59 of the 500

Filter the pool to entries whose frontmatter `description` length lies
in the [p30, p80] band (removes too-terse and too-verbose extremes that
make query authoring ambiguous), then `Random(42).sample(filtered, 59)`.
Frozen as `200bench/golds.json` (`G01..G59`). Gold selection is a pure
function of `pool_manifest.json` + `seed=42` — no human judgement is
exercised over which skills become golds.

#### Queries — 200 hand-authored English prompts

Frozen as `200bench/queries.yaml`:

- 150 in-distribution: 90 single-gold (`k=1`) + 45 two-gold (`k=2`) +
  15 three-gold (`k=3`)
- 50 null: 25 small-talk + 15 dev-trap + 10 adversarial
- Length policy: 30 % short (≤40c) / 50 % medium (41–140c) /
  20 % long (141–400c); hard ceiling 400 chars
- Anti-leakage guard: a pre-commit hook rejects any query containing a
  ≥12-character substring lifted verbatim from any SKILL.md body, so
  measured coverage cannot be inflated by surface-form leakage.

### 2.2 Evaluation conditions

Six conditions, all scored against the same 200 queries and 59 golds:

| Condition | Policy |
|---|---|
| `vanilla-codex` | Codex's `<skills_instructions>` block (`codex-rs/core-skills/src/render.rs`). Char-budget `min(ctx_window × 0.02, 8 000)`. Wire format: `- name: description (file: path)`; if even minimum-cost lines (no description) exceed budget, alphabetically order them and drop the overflow tail. Otherwise distribute description chars one at a time across all skills in round-robin order. |
| `vanilla-claude` | Claude Code's flat catalog (`code.claude.com/docs/en/skills`). Every skill name always emitted. Per-entry char cap: each description truncated to `maxSkillDescriptionChars` (default 1 536) at the source. Token budget = `ctx_window × skillListingBudgetFraction` (default 0.01). Under budget pressure, "descriptions for the skills you invoke least are dropped first"; under our fresh-user assumption (every count = 0) ties break alphabetically. **In our fixture** the 1,536-char cap is never triggered — pool descriptions cap at 509 chars — so the cap is a structural correctness fix that doesn't change measured numbers. |
| `vanilla-gemini` | Gemini's catalog (`google-gemini/gemini-cli` `packages/core/src/prompts/snippets.ts::renderAgentSkills`). No budget, no cap. Every enabled skill emitted as an XML `<skill>` element with three fields — `<name>`, `<description>`, `<location>` — wrapped in an `<available_skills>` block under an `# Available Agent Skills` markdown header. |
| `mega-tron-bge-m3` | mega-tron Router + `BAAI/bge-m3` (1024-dim multilingual) |
| `mega-tron-skillret` | mega-tron Router + `ThakiCloud/SKILLRET-Embedding-0.6B` (Qwen3-based, domain fine-tune) |
| `mega-tron-bge-small` | mega-tron Router + `BAAI/bge-small-en-v1.5` (384-dim English) |

The mega-tron router runs prompt-aware ranking + dynamic-K (abstain on
nulls, elbow + entropy-driven K window on in-distribution). All vanilla
simulators are reproduced bit-faithfully from each CLI's public source
(see `benchmarks/routing/vanilla_sim.py`). The 200 K context-window
preset is applied uniformly so token budgets are apples-to-apples.

To probe scaling behaviour we evaluate every condition at three pool
sizes: 59 (golds only), 183 (golds + 124 random non-golds), 500 (full).
Subset selection is deterministic (`seed=42`).

**Latency setup.** For each mega-tron condition we additionally
measure per-query wall-clock latency at pool=500, covering the full
routing pipeline: `embed(query) → cosine matmul → dynamic-K → catalog
render`. The first three queries are warm-up (discarded) so that
model-load and JIT compile costs don't contaminate timing. We report
mean / p50 / p95 / p99 across the remaining 197 queries
(`benchmarks/routing/measure_latency.py`). Vanilla conditions are
omitted from the latency table because their catalog is
prompt-independent — render time is a one-time cost amortised across
every prompt of a session (≪ 1 ms per call once cached).

### 2.3 Evaluation criterion — Coverage score

For each query with gold set `G` (∅ for nulls) and emitted-name set `P`
(every skill whose name lands in the catalog the model sees), the
per-query **coverage score** is

```
coverage(q) = |G ∩ P| / |G|         if |G| > 0
            = 1.0                    if |G| = 0 and |P| = 0   (correct abstain)
            = 0.0                    if |G| = 0 and |P| > 0   (false-positive null)
```

This is partial-credit recall over the *names* the model can see — a
skill counts as "covered" the moment its name reaches the catalog, even
if its description got truncated. We report the macro-mean across all
200 queries plus per-stratum slices (in-distribution / null /
false-positive-on-null rate) and the mean catalog-token cost.

## 3. Results

### 3.1 Coverage score by pool size

Macro-mean across all 200 queries.

| Condition | pool=59 | pool=183 | pool=500 |
|---|---|---|---|
| vanilla-codex | 0.708 | 0.185 | **0.029** |
| vanilla-claude | 0.750 | 0.750 | 0.750 |
| vanilla-gemini | 0.750 | 0.750 | 0.750 |
| mega-tron-bge-m3 | 0.879 | 0.873 | 0.840 |
| mega-tron-bge-small | 0.957 | 0.934 | 0.884 |
| **mega-tron-skillret** ★ | **0.955** | **0.935** | **0.892** |

### 3.2 Coverage broken out — in-distribution (150 queries) vs null (50 queries)

The macro-mean above hides the trade-off each policy is actually
making. The two columns per pool size below show, separately:
- **in-dist** — recall on the 150 prompts whose gold set is non-empty
  (higher is better)
- **null** — fraction of the 50 null prompts on which the policy
  correctly emits an empty catalog (higher is better; `1 − FP_null`)

| Condition | pool=59 in / null | pool=183 in / null | pool=500 in / null |
|---|---|---|---|
| vanilla-codex | 0.944 / 0.000 | 0.247 / 0.000 | 0.039 / 0.000 |
| vanilla-claude | 1.000 / 0.000 | 1.000 / 0.000 | 1.000 / 0.000 |
| vanilla-gemini | 1.000 / 0.000 | 1.000 / 0.000 | 1.000 / 0.000 |
| mega-tron-bge-small | 0.956 / 0.960 | 0.946 / 0.900 | 0.926 / 0.760 |
| mega-tron-bge-m3 | 0.839 / **1.000** | 0.837 / **0.980** | 0.813 / **0.920** |
| **mega-tron-skillret** ★ | **0.953** / 0.960 | **0.940** / 0.920 | **0.916** / 0.820 |

At pool=59 Codex catches 94.4 % of in-distribution golds because the
budget can still squeeze in every skill's name (no skill is dropped
entirely yet). At pool=183 the minimum-cost lines (`- name: (file: path)`)
already exceed the 4 000-char budget, so the alphabetically-late skills
get truncated off the catalog entirely — coverage falls to 24.7 %.
At pool=500 only the alphabetically-earliest ~56 skills survive, so
just 3.9 % of in-distribution golds remain reachable. None of the three
vanilla policies can abstain on null prompts.

Vanilla Claude / Gemini get a perfect 1.000 on in-distribution (every
name is always in the catalog) but a perfect 0.000 on nulls (they can
never abstain). mega-tron trades a few in-distribution points for a
near-perfect null abstain — that's why its macro-mean clears 0.750.

### 3.3 Token cost by pool size

| Condition | pool=59 | pool=183 | pool=500 |
|---|---|---|---|
| vanilla-codex | 1 193 | 1 157 | 1 191 |
| vanilla-claude | 1 972 | 2 000 | 3 400 |
| vanilla-gemini | 6 005 | 17 957 | 49 455 |
| mega-tron-bge-m3 | 112 | 145 | 208 |
| mega-tron-bge-small | 312 | 400 | 527 |
| **mega-tron-skillret** ★ | **106** | **124** | **157** |

Codex's catalog cost is flat (~1 200 tokens) across all pool sizes
because the char-budget hard cap is the binding constraint — once
the 4 000-char ceiling is hit, more skills just push earlier ones
out of the catalog rather than enlarging it. Gemini's catalog cost
scales linearly with pool size at ~99 tokens/skill — the XML wire
format (`<skill><name>..</name><description>..</description><location>..</location></skill>`)
imposes ~30 tokens of structural overhead per entry on top of the
description text itself.

#### Coverage-per-token efficiency (baseline = mega-tron-skillret)

Define efficiency as covered skill-slots delivered per 1 000 catalog
tokens consumed:

```
efficiency = 1000 × coverage / mean_catalog_tokens
```

Three tables — one per pool size — show every row's raw coverage,
raw mean token cost, that row's efficiency, and the ratio
`skillret_efficiency / row_efficiency`. The ratio reads as
**"how many times more coverage-per-token mega-tron-skillret delivers
than this row"** — values above 1.0× mean skillret wins, below 1.0×
means the other row wins.

**Pool = 59**

| Condition | coverage | tokens | efficiency | skillret / row |
|---|---|---|---|---|
| vanilla-codex | 0.708 | 1 193 | 0.59 | **15.2×** |
| vanilla-claude | 0.750 | 1 972 | 0.38 | **23.8×** |
| vanilla-gemini | 0.750 | 6 005 | 0.12 | **72.4×** |
| mega-tron-bge-small | 0.957 | 312 | 3.06 | 3.0× |
| mega-tron-bge-m3 | 0.879 | 112 | 7.82 | 1.2× |
| **mega-tron-skillret** (baseline) ★ | **0.955** | **106** | **9.04** | **1.0×** |

**Pool = 183**

| Condition | coverage | tokens | efficiency | skillret / row |
|---|---|---|---|---|
| vanilla-codex | 0.185 | 1 157 | 0.16 | **47.1×** |
| vanilla-claude | 0.750 | 2 000 | 0.38 | **20.1×** |
| vanilla-gemini | 0.750 | 17 957 | 0.04 | **180.2×** |
| mega-tron-bge-small | 0.934 | 400 | 2.34 | 3.2× |
| mega-tron-bge-m3 | 0.873 | 145 | 6.02 | 1.3× |
| **mega-tron-skillret** (baseline) ★ | **0.935** | **124** | **7.52** | **1.0×** |

**Pool = 500**

| Condition | coverage | tokens | efficiency | skillret / row |
|---|---|---|---|---|
| vanilla-codex | 0.029 | 1 191 | 0.02 | **231.4×** |
| vanilla-claude | 0.750 | 3 400 | 0.22 | **25.7×** |
| vanilla-gemini | 0.750 | 49 455 | 0.02 | **373.7×** |
| mega-tron-bge-small | 0.884 | 527 | 1.68 | 3.4× |
| mega-tron-bge-m3 | 0.840 | 208 | 4.03 | 1.4× |
| **mega-tron-skillret** (baseline) ★ | **0.892** | **157** | **5.67** | **1.0×** |

Reading the tables:
- Against **vanilla-codex**, the gap explodes with pool size:
  **15.2× → 47.1× → 231.4×**. Codex's token cost stays flat (~1 200)
  but its coverage collapses (0.71 → 0.19 → 0.03) as more skills get
  pushed out of the alphabetically-ordered budget cap.
- Against **vanilla-gemini**, `skillret`'s efficiency advantage scales
  with pool size: **72.4× → 180.2× → 373.7×**. Gemini's uncapped XML
  catalog grows linearly at ~99 tokens/skill (XML structure + location
  path on every entry), reaching 49 K tokens at pool=500 while
  skillret's catalog grows only ~1.5×.
- Against **vanilla-claude**, `skillret` is **20 – 26× more efficient**
  at every pool size.
- Within mega-tron, `skillret` beats `bge-small` by **3.0 – 3.4×** and
  `bge-m3` by **1.2 – 1.4×** on efficiency; it pairs the highest
  coverage with the lowest token cost. `bge-small` is the *latency*
  optimum (§3.4), not the efficiency optimum.

### 3.4 Per-query latency (pool=500, warm cache)

Wall-clock time for the full routing pipeline (`embed → cosine matmul
→ dynamic-K → catalog render`), measured over 200 queries with 3
warm-up calls discarded.

| Condition | mean | p50 | p95 | p99 | mean K |
|---|---|---|---|---|---|
| mega-tron-skillret | 77.8 ms | 67.7 ms | 129.4 ms | 139.0 ms | 2.5 |
| mega-tron-bge-m3 | 52.5 ms | 44.2 ms | 83.3 ms | 107.6 ms | 3.3 |
| mega-tron-bge-small ★ | **15.7 ms** | **12.3 ms** | 34.4 ms | 39.6 ms | 7.0 |

All three sit well under the typical first-token latency of a frontier
LLM (~300–800 ms), so the router is never the bottleneck end-to-end.
`bge-small` is the latency-optimal pick (5× faster than `skillret`)
at the cost of ~0.8 pp coverage and ~3× more catalog tokens; `skillret`
is the coverage-and-tokens-optimal pick.

## 4. Implications

- **Vanilla Codex's alphabetical char-budget policy collapses as the
  pool grows.** Codex's 4 000-char budget (in 200K-context mode) is
  the binding constraint — every skill's minimum-cost line
  (`- name: (file: path)`) consumes ~75 chars, so the catalog
  saturates at ~55 skills. Past that point, alphabetically-late skills
  are dropped entirely. At pool=59 the budget still fits every skill's
  name and Codex catches 94.4 % of in-distribution golds (mean
  coverage 0.708). At pool=183 only ~55 of 183 skills survive
  (in-dist 0.247). At pool=500 in-dist falls to 0.039 and overall
  mean to 0.029. Codex's static policy is structurally unfit for any
  skill library larger than ~50 entries.

- **Vanilla Claude and Gemini hit a coverage ceiling of 0.750 regardless
  of pool size.** Both emit every name in the pool, so in-distribution
  coverage is a perfect 1.0 (all 150 prompts) but null abstain is
  impossible (all 50 null prompts always pull at least one false-positive
  skill name into context). 150·1.0 + 50·0.0 = 150 → 0.750 / 200.

- **mega-tron clears the vanilla ceiling at every pool size.** Best
  setting (`skillret` + abs_floor 0.40) reaches 0.892 coverage on the
  full 500-skill pool — **+14.2 pp over Claude / Gemini** and
  **+86.3 pp over Codex** — while emitting only 157 tokens of catalog
  context per turn.

- **Token-cost scaling separates the policies sharply.** As the pool
  grows from 59 → 500, Gemini's XML catalog explodes 8.2× (6 005 →
  49 455 tokens), Claude grows 1.7× (1 972 → 3 400), Codex stays flat
  at ~1 200 by silently dropping skills, and mega-tron grows only
  ~1.5× (106 → 157) because dynamic-K cuts at the actual confidence
  elbow regardless of how many distractors are present. At pool=500,
  mega-tron-skillret uses **315× fewer tokens than Gemini, 22× fewer
  than Claude, and 7.6× fewer than Codex** — while scoring strictly
  higher on coverage.

- **Pool size is where prompt-aware routing earns its keep.** The
  vanilla policies were good enough on 59-skill libraries (Codex 0.708,
  Claude / Gemini 0.750). They become structurally broken (Codex) or
  wasteful (Gemini, Claude) once the user installs hundreds of skills —
  which is the regime real users land in. The larger the pool, the
  larger mega-tron's advantage on both axes simultaneously.
