# mega-tron Routing & Skill Lifecycle

This document describes how mega-tron decides **which** skills to surface
for each turn, **how many** to surface, and **how a skill's evidence
record governs whether it stays in the candidate pool at all**.

The flow is top-down:

1. [Routing pipeline overview](#1-routing-pipeline-overview) — the five
   stages from prompt to top-K injection.
2. [Evidence-blended re-rank](#2-evidence-blended-re-rank) — how
   cosine is decorated with four verdict-derived signals.
3. [Skill status lifecycle](#3-skill-status-lifecycle) — active /
   suspect / archived and the `_refresh_status` algorithm.
4. [Dynamic K](#4-dynamic-k) — how the score distribution shape
   picks K per turn.
5. [Configuration & operations](#5-configuration--operations) — env
   vars, knobs, when to disable, future work.

---

## 1. Routing pipeline overview

```
user prompt
    │
    ▼
┌────────────────────────────────────────────────────────────┐
│ ① Embed query                                              │
│    asymmetric: query gets instruction prefix,              │
│    documents stay plain (BGE-M3 / SkillRet / etc.)         │
└────────────────────────────────────────────────────────────┘
    │
    ▼
┌────────────────────────────────────────────────────────────┐
│ ② Semantic score per skill                                 │
│    semantic = max( cos(q, full_doc), cos(q, name) )        │
│    fast matmul over cached (N, dim) matrix                 │
└────────────────────────────────────────────────────────────┘
    │
    ▼
┌────────────────────────────────────────────────────────────┐
│ ③ Evidence-blended re-rank   (when MEGA_EVAL_BLEND=1)      │
│                                                            │
│    final = ( semantic                                      │
│            + 0.10 × beta_smoothed_count_bonus              │
│            + 0.15 × (helpful_ctx_match                     │
│                      − 1.5 × harmful_ctx_match)            │
│            + 0.10 × (related_helpful_max                   │
│                      − related_harmful_max)                │
│            ) × status_multiplier                           │
│              { active: 1.0, suspect: 0.5, archived: −1.0 } │
└────────────────────────────────────────────────────────────┘
    │
    ▼
┌────────────────────────────────────────────────────────────┐
│ ④ Sort by final score                                      │
│    archived skills (final = −1) sink below any real score  │
│    → effectively excluded                                  │
└────────────────────────────────────────────────────────────┘
    │
    ▼
┌────────────────────────────────────────────────────────────┐
│ ⑤ Dynamic K                                                │
│    cut at min(K, top_k_cap) using score distribution shape │
└────────────────────────────────────────────────────────────┘
    │
    ▼
top-K skills injected into the host's native catalog point
```

The two decisions are **orthogonal**:

- **Stages ③–④** are *slow-changing, cumulative*: many verdicts over
  time decide whether a skill earns × 1.0 / × 0.5 / × −1.0, and how
  much its additive blend terms move the needle.
- **Stage ⑤** is *fast-changing, per-turn*: a single query's score
  distribution decides how many ranked candidates to actually surface.

The blend layer (`ranker.adjusted_score`) is pure — no I/O, no globals
besides weights. Cold-start skills (no verdicts) contribute zero through
every additive term, so `final == semantic` and they're never penalized
vs. evaluated peers.

---

## 2. Evidence-blended re-rank

### The four signals

| Signal | Default weight | Source | Cold-start |
|---|---|---|---|
| `semantic` | — (base) | `max(cos(q, doc), cos(q, name))` | — |
| `count_bonus` | `W_COUNT = 0.10` | Beta-Bernoulli smoothed helpful rate, ramped to full strength at 10 invocations | 0 |
| `context_match` | `W_CONTEXT = 0.15` (with `W_HARM = 1.5` asymmetry inside) | best `cos(q, ctx)` over `helpful_contexts`, minus 1.5× best over `harmful_contexts` | 0 |
| `related_verdict` | `W_RELATED = 0.10` | best `cos(q, past_verdict_reason)` lookup in `verdict_embeddings.npz`, split by polarity | 0 |
| `status_multiplier` | — (gate) | `active=1.0` / `suspect=0.5` / `archived=−1.0` | active = 1.0 |

All weights are env-overridable: `MEGA_EVAL_COUNT_W`,
`MEGA_EVAL_CONTEXT_W`, `MEGA_EVAL_HARM_W`, `MEGA_EVAL_RELATED_W`. The
master switch `MEGA_EVAL_BLEND=0` reverts ranking to pure cosine.

### Why these weights

- **Harmful is weighted 1.5× helpful** inside `context_match`. False
  positives (model picks a broken skill) are more expensive than false
  negatives (model misses a working one), so HARMFUL evidence punishes
  asymmetrically.
- **`W_COUNT = W_RELATED = 0.10`** by design — one high-similarity past
  HELPFUL verdict carries roughly the same weight as a fully-warm
  helpful count signal. This keeps the per-verdict embedding signal
  from drowning the slower-moving frequency signal.
- **`semantic` stays dominant.** The additive contributions are
  bounded (count_bonus ∈ [−0.05, +0.05]; the others by cosine ranges)
  so semantic relevance always wins on a clear gap, with evidence
  breaking ties.

### Short-circuit for archived

When `meta.status == "archived"`, the breakdown short-circuits
(`ranker.py` lines 219-238):

- All additive terms reported as 0 (saves a matmul over context
  embeddings).
- `final = −1.0` — the archived sentinel.
- Sort naturally pushes them below any real score → effectively
  excluded from candidate sets.

Recovery from archived is **one-way**: a manual edit of
`mega_meta.status` in the skill's SKILL.md frontmatter.

### `why` introspection

When a skill's rank surprises you:

```bash
mega-tron why "validate HMAC webhook" webhook-signer
#   semantic          +0.81  (full=+0.81, name=+0.62)
#   count_bonus       +0.04  (h=8, ha=0, raw=+0.40, w=0.10)
#   context_match     +0.12  (help=+0.83, harm=+0.00 × 1.50, w=0.15)
#   related_verdict   +0.07  (help_max=+0.71, harm_max=+0.00, w=0.10)
#   status_mult       × 1.00  (active)
#   ─────────────────────────
#   final             +1.04
```

Every per-term contribution is exposed so you can pinpoint *which*
signal carried the rank — useful when reasoning about whether a
suspicious top pick came from semantic strength, verdict bias, or
the related-verdict embedding lookup.

---

## 3. Skill status lifecycle

Each skill carries an evidence block in its SKILL.md frontmatter:

```yaml
mega_meta:
  helpful_count: 12
  harmful_count: 2
  helpful_contexts: [...]      # capped at 3, oldest dropped
  harmful_contexts: [...]
  status: active               # active | suspect | archived
  consecutive_harmful: 0       # streak counter for archived rule
  last_session_id: "0193..."
  last_updated: "2026-05-21T07:54:33Z"
```

The Stop-hook scans the transcript at session end and emits one
verdict per skill: `HELPFUL` / `HARMFUL` / `NEUTRAL`.
Each verdict mutates the block via `apply_verdict` and then
`_refresh_status` re-classifies the skill.

### Verdict effects on the counters

| Verdict | helpful_count | harmful_count | consecutive_harmful | context append |
|---|---|---|---|---|
| `HELPFUL`     | +1 | — | **reset to 0** | helpful_contexts |
| `HARMFUL`     | — | +1 | **+1** (accumulate) | harmful_contexts |
| `NEUTRAL`     | — | — | unchanged (streak preserved, not extended) | — |

When the model has no evidence to ground a verdict, it omits the
`<skill-used .../>` tag for that skill entirely — silence is the
"no signal" escape hatch. `NEUTRAL` is "skill ran but didn't move the
needle" — neither counter changes, but a HARMFUL streak in progress
is not broken.

### Thresholds

From `verdicts/mega_meta.py`:

| Constant | Value | Role |
|---|---|---|
| `AUTO_ARCHIVE_THRESHOLD` | 3 | Consecutive HARMFUL verdicts (no HELPFUL in between) that flip status to archived |
| `MIN_INVOCATIONS_FOR_STATUS` | 5 | Minimum total verdicts before suspect classification activates (cold-start protection) |
| `HARMFUL_COUNT_THRESHOLD` | 3 | Absolute harmful count `> 3` (i.e. ≥ 4) triggers suspect |
| `HARMFUL_RATIO_THRESHOLD` | 0.3 | Harmful ratio `> 0.3` triggers suspect |
| Recovery `ratio` ceiling | ≤ 0.15 | Must hold to recover suspect → active |
| Recovery `harmful_count` ceiling | ≤ 1 | Must also hold (AND with above) |

### The `_refresh_status` algorithm

Called after every HELPFUL/HARMFUL verdict:

```python
def _refresh_status(self):
    # STEP 1 — archived check (highest priority, short-circuits)
    if self.consecutive_harmful >= 3:
        self.status = "archived"
        return                               # no recovery path below ever runs

    # STEP 2 — cold-start guard
    total = self.helpful_count + self.harmful_count
    if total < 5:
        return                               # not enough data; status untouched

    # STEP 3 — escalate to suspect
    ratio = self.harmful_count / total
    if self.harmful_count > 3 or ratio > 0.3:    # OR
        self.status = "suspect"

    # STEP 4 — recover suspect → active
    elif self.status == "suspect" and ratio <= 0.15 and self.harmful_count <= 1:
        self.status = "active"               # AND — both must hold
```

### Transition graph

```
                       initial
                          │
                          ▼
                    ┌─────────┐
       ┌────────────│ active  │◀──────────┐
       │            └─────────┘           │
       │                 │                │
       │   ratio > 0.3   │                │  ratio ≤ 0.15
       │   OR            │                │  AND
       │   harm > 3      │                │  harm ≤ 1
       │   (total ≥ 5)   │                │  (from suspect only)
       │                 ▼                │
       │            ┌─────────┐           │
       │            │ suspect │───────────┘
       │            └─────────┘
       │                 │
       │  3 consecutive  │  3 consecutive
       │  HARMFUL        │  HARMFUL
       │  (any HELPFUL   │  (any HELPFUL
       │  resets streak) │  resets streak)
       ▼                 ▼
              ┌──────────────────┐
              │     archived     │      one-way
              └──────────────────┘
                       │
                       ▼
              manual YAML edit only
                (mega_meta.status)
```

### Behavioral notes

- **active → archived can be direct.** STEP 1 doesn't read the current
  status. A fresh skill whose first 3 verdicts are all HARMFUL
  (total = 3 < 5, so STEP 2 would return) still gets archived because
  STEP 1 fires first. Same when an active skill in normal usage hits
  a 3-in-a-row run of failures.
- **Cumulative HARMFUL alone never triggers archived.** With 50
  HARMFUL and 1 HELPFUL interleaved, status sits at suspect (high
  ratio) but never archives — the streak counter keeps resetting.
  Archived is strictly a **recent consecutive failure** signal.
- **Suspect is the soft-eviction lever.** A suspect skill keeps its
  position in the candidate set but is multiplied by 0.5; it usually
  loses the dynamic-K cut to active competitors with comparable
  semantic scores, without being deleted outright. This gives a
  damaged skill a chance to recover when a fixed version finally
  evaluates HELPFUL.
- **Recovery is intentionally strict.** Suspect → active requires
  *both* `ratio ≤ 0.15` and `harmful_count ≤ 1`. The absolute cap is
  what makes recovery possible at all — a skill with many old
  harmful verdicts can never recover by ratio alone, which is the
  right behavior when a skill has demonstrated a sustained failure
  mode.
- **NEUTRAL preserves streaks.** Pattern `H, M, M, N, M` archives on
  the final M because NEUTRAL doesn't break the streak
  (`consecutive_harmful` reaches 3). NEUTRAL is "no signal," not
  "exoneration."

### Re-syncing status from the verdict store

When verdicts are deleted via the dashboard, `resync_from_store`
rebuilds counters from the SQLite verdicts table (the natural-language
contexts are preserved verbatim — they don't map 1:1 to verdict rows)
and re-runs `_refresh_status`. This keeps the frontmatter — the
canonical source of cumulative counts — consistent with the underlying
time-series after any history edit.

---

## 4. Dynamic K

mega-tron's router decides how many ranked skills to surface for each
turn. Rather than always returning a fixed K, it inspects the shape of
the score distribution and picks K dynamically — anywhere from 0 to
the embedder-tier max.

### Why dynamic K?

When the router asks "given this query, what are the top skills?", the
answer's *shape* tells you something about how confident the router is:

- **Sharp peak** — one skill clearly leads. Sending K=10 wastes 7
  slots of prompt context on long-tail noise.
- **Flat distribution** — many skills score similarly. Sending K=1
  forces the model to guess on a single candidate that may not be
  the best.
- **Weak, flat distribution** — nothing is really relevant. Sending
  any K teaches the model to use the wrong tool.

A static K=3 treats all three identically. Dynamic K reacts to each.

### The three signals

After the router ranks every cached skill, we take the top-20 raw
cosine scores (already sorted descending) and compute three things.

#### Signal 1: `z_top1` — "how much does the leader stand out?"

```
mean   = average(scores)
sd     = stddev(scores)
z_top1 = (scores[0] - mean) / sd
```

`z_top1` measures the leader in *standard deviations* above the mean.
A value of 3.0 means "the top score is 3 σ above the rest" — a strong
peak. A value near 0 means "the top score is barely distinguishable."

**Why z-units, not raw cosine?** Different embedders score on different
scales. SkillRet's in-distribution top-1 sits around 0.45; BGE's sits
around 0.78 for the same kind of query. A raw threshold like
"top1 < 0.30" would never trigger on BGE but always trigger on weaker
models. Z-units strip that out — a strong peak is a strong peak
regardless of the embedder.

#### Signal 2: `z_ent` — "is the distribution peaky or flat?"

We softmax the top-10 z-scores and take the Shannon entropy of the
resulting probability distribution:

```
p     = softmax(z[:10])         # temperature 1.0
z_ent = -Σ p_i * log(p_i)
```

- `z_ent ≈ 0.5` — one z-score dominates the softmax; distribution
  very peaky.
- `z_ent ≈ 1.5` — moderate spread.
- `z_ent ≈ 2.3` — softmax is near-uniform across all 10; distribution
  flat (max possible is `ln(10) ≈ 2.30`).

Since the z-scores already have mean 0 and unit variance, no extra
temperature parameter is needed — softmax at T=1.0 reads off the
distribution shape directly.

#### Signal 3: `raw_gap` — "where does the score cliff drop?"

We look at consecutive raw cosine gaps in the top-9:

```
gaps[i] = scores[i] - scores[i+1]
elbow   = argmax(gaps)        # index of the biggest gap
```

A big gap between, say, scores[2] and scores[3] means the top-3 form
a coherent cluster and everything below is a different (worse) cluster.
That elbow index is a natural place to cut.

**Why raw cosine here, and not z?** Z-normalization happens *within
one query's* score list. Inside that list, raw gaps and z gaps are
linearly related — the elbow position is the same either way. But
keeping raw cosine here makes the gap magnitudes more interpretable
for humans inspecting telemetry ("scores dropped from 0.62 to 0.41 at
index 2").

### The decision rule (5 branches, first-match wins)

```
# A0. Hard nonsense filter (embedder-specific abs_floor)
if scores[0] < abs_floor:
    K = 0  (abs-floor)

# A. Abstain — nothing is meaningfully relevant
if z_top1 < 1.8 AND z_ent > 1.85:
    K = 0  (uniform-null)

# B. Very ambiguous — distribution nearly uniform
if z_ent > 2.1:
    K = 10  (very-ambiguous)

# C. Ambiguous — distribution flat
if z_ent > 1.7:
    K = 5   (ambiguous)

# D. Confident — peaky distribution; cut at the elbow
elbow = argmax(raw gaps in top-9)
K = clamp(elbow + 1, k_min=2, k_max=8)
```

The `abs_floor` branch is **embedder-specific** and disabled by default
(`abs_floor=None`). When the router knows which embedder is loaded, it
automatically picks a floor from `EMBEDDER_PROFILES`:

| Embedder | Tier | `abs_floor` |
|---|---|---|
| BGE-M3 | strong | 0.60 |
| SkillRet-0.6B | strong | 0.40 |
| Qwen3-0.6B | strong | 0.35 |
| BGE-small-v1.5 | weak | 0.70 |
| BGE-large-v1.5 | medium | 0.65 (estimated) |
| e5-small | weak | 0.40 (estimated) |
| e5-base | medium | 0.45 (estimated) |

Floors were set via empirical testing against the routing benchmark.
Rows marked *estimated* were inferred from model family and not
measured directly; replace with empirical values once benchmarks
are run for them.

The abstain *order matters*: `abs_floor` fires first because it's
deterministic and cheap; `uniform-null` (z gates) catches the cases
where the embedder happens to find a "lucky" similar skill for a
nonsense query.

### Strength tiers — adapting K to embedder quality

Stronger embedders rank the gold skill at rank 1 more often, so K can
stay small without losing recall. Weaker embedders push the gold to
rank 5-10 on average, so K has to widen to compensate. We capture this
asymmetry by tiering embedders into **strong / medium / weak** with
matched K bounds:

| Tier | k_min | k_max | k_ambig | k_very_ambig | Rationale |
|---|---|---|---|---|---|
| strong | 2 | 10 | 5 | 10 | recall@10 plateau ≈ 90% — K=10 covers most golds |
| medium | 3 | 15 | 7 | 12 | recall@K climbs more slowly; wider safety margin |
| weak | 5 | 20 | 10 | 15 | recall@5 ≈ 66% only — must surface more candidates |

Measured recall@K on the SkillRet test split:

| Embedder | recall@1 | recall@5 | recall@10 | recall@20 |
|---|---|---|---|---|
| SkillRet (strong) | 70% | 88% | 90% | 92% |
| BGE-M3 (strong, est.) | ~75% | ~92% | ~95% | — |
| BGE-small (weak) | 45% | 66% | 75% | 79% |

So if you swap mega-tron's default (BGE-M3) for BGE-small to save
build time, dynamic_k automatically widens K from 2-10 to 5-20 to
keep the gold inside the staged window. No user action needed —
`profile_for(embedder.model_id)` handles it.

### Worked examples

**Example 1: clean peak**

```
scores = [0.78, 0.62, 0.58, 0.41, 0.38, 0.36, 0.35, 0.34, 0.33, 0.32]
z_top1 = 2.23   (top is 2.2 σ above mean — fairly strong)
z_ent  = 1.64   (some spread, but a peak is visible)
gaps   = [0.16, 0.04, 0.17, 0.03, 0.02, 0.01, ...]
elbow  = 2      (biggest gap is 0.17 between scores[2] and scores[3])
→ K = 3, reason = "gap-cut@2"
```

**Example 2: null prompt**

```
scores = [0.30, 0.30, 0.30, 0.30, 0.30, 0.30, 0.30, 0.30, 0.30, 0.30]
z_top1 = 0.00   (no peak at all)
z_ent  = 2.30   (perfectly uniform → max entropy)
→ K = 0, reason = "uniform-null"
```

**Example 3: ambiguous query (JWT — auth? crypto? token?)**

```
scores = [0.55, 0.53, 0.51, 0.49, 0.47, 0.45, 0.43, 0.41, 0.39, 0.37]
z_top1 = 2.10   (mild peak)
z_ent  = 1.82   (somewhat flat)
→ K = 5, reason = "ambiguous"  (entropy above 1.7)
```

**Example 4: overwhelming leader**

```
scores = [0.82, 0.41, 0.40, 0.39, 0.38, 0.37, 0.36, 0.35, 0.34, 0.33]
z_top1 = 3.00   (strong peak)
z_ent  = 1.05   (very concentrated)
gaps   = [0.41, 0.01, 0.01, ...]
elbow  = 0      (biggest gap is right after position 0)
→ K = clamp(1, 2, 8) = 2, reason = "gap-cut@0"
```

The `k_min=2` floor catches this case. K=1 would be tempting (one
clearly-best skill) but leaves no fallback if the router's leader is
wrong — having position 2 as a backup is worth the extra slot.

### How the thresholds were chosen

We measured the distribution of `z_top1` and `z_ent` on:

- 100 in-distribution queries (queries whose gold skill exists in the
  cached pool) from the SkillRet test split.
- 50 synthetic null queries (general-knowledge questions with no
  relevant skill: "What is the capital of France?", "Tell me a joke
  about cats", etc.).

…against two different embedders:

- `ThakiCloud/SKILLRET-Embedding-0.6B` (instruction-tuned Qwen3)
- `BAAI/bge-small-en-v1.5` (generic BGE retrieval)

The key finding: in z-space the in-dist and null distributions overlap
considerably (z_top1's separation is only ~0.9 σ on SkillRet, ~0.4 σ
on BGE), so abstain thresholds that catch many true nulls also catch
many true in-dist queries.

We chose `(abstain_z_top1=1.8, abstain_z_ent=1.85)` as a conservative
operating point: it false-abstains ~2-3% of in-distribution queries
across both embedders, while catching ~2-4% of true nulls. The
asymmetric cost model — false abstain wastes a routing opportunity but
false non-abstain just wastes K cheap tokens — argues for staying
conservative.

The ambig and very-ambig gates `(1.7, 2.1)` were picked so that:

- ~25% of in-dist queries land in the ambiguous branch (queries where
  the router really isn't sure which of 3-5 skills is correct);
- ~5% land in very-ambiguous;
- the remaining ~70% are confident and use the raw-gap elbow.

### Why hybrid (z + raw-cosine)?

Z-normalization buys you **embedder invariance** but costs you some
**separation power**:

| Signal | Embedder-invariant? | Separates IN from NULL? |
|---|---|---|
| raw top1 | No (SkillRet ~0.45, BGE ~0.78) | Yes, strongly on BGE (d≈3.6) |
| z_top1   | Yes                            | Weak (d ≈ 0.4–0.9) |
| z_ent    | Yes                            | Weak (d ≈ 0.3–1.0) |
| raw gap  | Doesn't matter (intra-query)   | N/A (used for elbow only) |

Strong embedders like BGE encode in-distribution queries far enough
above null queries that the *absolute* gap is very clean. Z-normalizing
flattens that gap. So we use:

- **Z-space** for *abstain* and *ambiguous* decisions — we want one
  set of thresholds that ports across embedders.
- **Raw cosine** for the *elbow* decision — gaps inside one query
  are scale-free, so raw is just easier to interpret.

This is the only place in mega-tron where we deliberately mix the two
representations, and it's documented inline in `dynamic_k.py`.

### Silence penalty

A skill that gets surfaced repeatedly without ever earning a verdict
is the symptom of *cosine-only anchoring*: the router never sees a
signal back from `<skill-used>` tags, so the same skill keeps
winning the top slot turn after turn. The silence penalty is a
**continuous** down-shift applied to each candidate's score at
K-selection time, designed to edge silent candidates out of K and
let a fresher peer enter.

```
silence_penalty = α × log1p(surfaced) × max(0, 1 − verdict_density)
where verdict_density = (helpful + harmful) / max(1, surfaced)
```

| α (default = 0.02) | surfaced | helpful + harmful | penalty |
|---|---|---|---|
| 0.02 | 10 | 0 | 0.048 |
| 0.02 | 50 | 0 | 0.079 |
| 0.02 | 50 | 1 | 0.077 |
| 0.02 | 50 | 5 | 0.071 |
| 0.02 | 50 | 25 | 0.039 |
| 0.02 | 50 | 50 | 0.000 |

Two design notes:

- **Smooth, not a cliff.** A discrete `surfaced ≥ N` threshold
  would create a 19-vs-20 boundary problem. `log1p` keeps adjacent
  surface counts within a small bounded distance of each other.
- **Self-disarms with any verdict.** The `(1 − verdict_density)`
  factor goes to 0 as soon as evidence accumulates; we don't need
  a hand-coded recovery rule.

The penalty composes with the existing four-term blend:

```
score_for_k = final − silence_penalty(surfaced, helpful, harmful)
```

**Crucially the returned score stays unchanged.** Only the K
boundary moves: a sparse-evaluated candidate may get edged out of
K, but if it does enter K its displayed rank and score vs other
picks are untouched. `mega-tron why` shows the per-skill
`silence_penalty` row purely for transparency.

Override α via `MEGA_SILENCE_PENALTY_ALPHA` (set to `0` to disable
the penalty entirely — useful for A/B comparison or to revert to
the pre-penalty K policy).

---

## 5. Configuration & operations

### Dynamic-K config

Every threshold lives in `DynamicKConfig`:

```python
from mega_tron.dynamic_k import DynamicKConfig, dynamic_k, profile_for

# Auto-pick a config based on the embedder model id:
cfg = profile_for("BAAI/bge-small-en-v1.5")  # gives abs_floor=0.70

# Or build one explicitly:
cfg = DynamicKConfig(
    abs_floor=0.70,        # None to disable the hard floor
    abstain_z_top1=1.8,
    abstain_z_ent=1.85,
    very_ambig_z_ent=2.1,
    ambig_z_ent=1.7,
    k_ambig=5,
    k_very_ambig=10,
    k_min=2,
    k_max=8,
)

# Baseline call — no silence penalty applied. Mirrors the policy used
# by the agentic rerank path (where the LLM does its own selection).
k, reason = dynamic_k(scores, cfg=cfg)

# Silence-penalty call. `candidate_stats` is order-aligned with `scores`
# and carries `(surfaced, helpful, harmful)` per candidate. The penalty
# subtracts from a private score copy at K-selection time only; the
# caller's score list is untouched.
candidate_stats = [(53, 0, 0), (26, 8, 1), (49, 2, 0), ...]
k, reason = dynamic_k(scores, cfg=cfg, candidate_stats=candidate_stats)
```

Hosts wire it through `Router.rank(query, dynamic=True)`. When the
caller doesn't pass `dynamic_cfg`, the router calls `profile_for(
embedder.model_id)` automatically — so a known embedder gets both its
nonsense floor *and* its tier-appropriate K bounds for free. Unknown
embedders fall back to z-only abstain with strong-tier K bounds.

The chosen K and branch reason are exposed on `router.last_dynamic`
for telemetry.

### Blend weights — env vars

| Variable | Default | Effect |
|---|---|---|
| `MEGA_EVAL_BLEND` | `1` | `0` reverts ranking to pure cosine |
| `MEGA_EVAL_COUNT_W` | `0.10` | Weight on Beta-smoothed helpful-rate bonus |
| `MEGA_EVAL_CONTEXT_W` | `0.15` | Weight on `helpful_ctx − 1.5 × harmful_ctx` term |
| `MEGA_EVAL_HARM_W` | `1.5` | Asymmetry multiplier on harmful context match |
| `MEGA_EVAL_RELATED_W` | `0.10` | Weight on related-verdict embedding signal |
| `MEGA_SILENCE_PENALTY_ALPHA` | `0.02` | α for the K-selection silence penalty; set to `0` to disable |

### Verdict-embedding store auto-compaction

`verdict_embeddings.npz` accumulates one row per verdict, even when
reasons are semantically near-duplicates (e.g. one heavily-used skill
collecting many "validated webhook HMAC" / "verified webhook
signature" verdicts). The writer auto-fires `compact_embeddings()`
under **either** condition:

- **Disk-cost trigger.** `len(ves) > 10_000` — protects long-term
  memory residency. Sized for a five-years-of-active-use corpus.
- **Quality trigger.** `len(ves) > max(50, verdicts_table_count × 1.5)`
  — catches the silence-loop symptom where one busy skill grows the
  npz faster than other skills earn fresh evaluations. The 50-row
  floor keeps the trigger asleep during fresh-install warm-up where
  small-N ratios are unstable.

Both triggers call the same `compact()` with cosine threshold 0.95
(true paraphrases collapse, similar-topic verdicts survive). The
SQLite `verdicts` table is untouched — only the embedding store is
deduped — so regression analysis remains time-series-true.

### When to disable dynamic K

Add `--no-dynamic-k` to any host hook command to fall back to the
static `--top-k` behavior. Useful for:

- A/B comparing dynamic vs static in benchmarks.
- Workflows that explicitly want a fixed-size context window.
- Debugging when the policy looks like it's misbehaving on a specific
  query (you can always inspect `router.last_dynamic` to see why).

### What's not here (yet)

This document describes the **static** dynamic-K policy. A follow-up
`adaptive_k` module will:

- Persist `router.last_dynamic` decisions to disk.
- Pair each decision with the actual skill the host activated (from
  the Stop hook's `<skill-used/>` tag scan or equivalent).
- Periodically re-tune the eight thresholds from observed
  abstain-recall and gap-cut accuracy.

The static policy is deliberately the foundation: it has to be right
on its own before any adaptive layer can learn from it.
