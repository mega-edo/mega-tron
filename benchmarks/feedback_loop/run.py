"""Feedback-loop experiment runner.

Two conditions (C0 verdict-OFF, C3 verdict-ON), each over 6 rounds (R0..R5)
of 13 prompts, against Gemini gemini-3-flash-preview. Per round:

  1. MEASUREMENT — call MegaCore.route() for each prompt, record
     adjusted_score_breakdown() per ranked skill into c<X>_round_<r>.jsonl.
  2. LEARNING — call Gemini with a top-5 router-curated catalog, parse
     <skill-used verdict=.../> tags, persist verdicts to the sandbox store.

Sandbox isolation (load-bearing):
  - MEGA_TRON_STORE → /tmp/feedback_loop/<cond>/store.db
  - MEGA_TRON_VERDICT_EMBEDDINGS → /tmp/feedback_loop/<cond>/verdicts.npz
  - Gemini cwd → /tmp/feedback_loop/<cond>/workspace/
       which symlinks the 80 fixture SKILL.md dirs into .gemini/skills/
  - C0 additionally sets MEGA_EVAL_BLEND=0 so router skips verdict blending.

The user's real ~/.local/share/mega-tron/store.db and ~/.gemini/skills/
are never touched.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# mega-tron imports
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import re  # noqa: E402

from mega_tron.verdicts.writer import persist_verdicts  # noqa: E402


_SKILL_USED_RE = re.compile(
    r"<skill[-_]used\b(?P<attrs>(?:\s+[a-z_]+=[\"'][^\"']*[\"'])+)\s*/?>",
    re.IGNORECASE,
)
_ATTR_RE = re.compile(r"([a-z_]+)\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)


def _parse_skill_used_tags(text: str) -> list[dict]:
    """Pull `<skill-used name=... verdict=... reason=.../>` tags from plain text.

    Replaces the deprecated mega_tron.hosts.hermes.shim parser. Returns a
    list of {"name", "verdict", "reason"} dicts, one per tag found. Tags
    missing `name` are skipped; `verdict` and `reason` default to "".
    """
    out: list[dict] = []
    for m in _SKILL_USED_RE.finditer(text or ""):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(m.group("attrs") or "")}
        name = (attrs.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "verdict": (attrs.get("verdict") or "").strip(),
            "reason": (attrs.get("reason") or "").strip(),
        })
    return out


FIXTURE_ROOT = Path(__file__).resolve().parent
SANDBOX_ROOT = Path("/tmp/feedback_loop")
GEMINI_BIN = "/Users/gwanghoon/.npm-global/bin/gemini"
GEMINI_MODEL = "gemini-3-flash-preview"
TIMEOUT_S = 300  # per-turn Gemini timeout

def parse_stream_json(stream_stdout: str) -> dict:
    """Extract the final assistant text from a Gemini stream-json stdout.

    Gemini CLI emits one JSON object per line. The assistant's final reply
    arrives as a sequence of ``{"type":"message","role":"assistant",
    "delta": true, "content":"<chunk>"}`` lines whose contents
    concatenate (in order) into the full reply. Non-delta and
    non-assistant lines are skipped; malformed lines are ignored.

    Returns ``{"final_text": "..."}``. Empty stdout, all-malformed input,
    or no assistant chunks all yield ``{"final_text": ""}`` — the caller
    falls back to ``res["stdout"]`` in that case.

    Replaces the deleted ``benchmarks/hosts/gemini/run_bench.py`` helper.
    Verified against archived stream.json files in
    ``results/<TS>/raw/<cond>/`` — joined-content length matches the
    saved ``.text.md`` to within trailing whitespace.
    """
    chunks: list[str] = []
    for line in (stream_stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "message":
            continue
        if obj.get("role") != "assistant":
            continue
        if not obj.get("delta"):
            # Non-delta assistant messages (final assembled echo) would
            # duplicate the deltas; skip them.
            continue
        content = obj.get("content")
        if isinstance(content, str):
            chunks.append(content)
    return {"final_text": "".join(chunks)}


# ---------------------------------------------------------------------------
# Sandbox setup
# ---------------------------------------------------------------------------


@dataclass
class Sandbox:
    cond: str
    workspace: Path
    skills_dir: Path  # workspace/.gemini/skills
    store_path: Path
    verdict_npz: Path
    fake_home: Path

    @property
    def env_overrides(self) -> dict[str, str]:
        env = {
            "MEGA_TRON_STORE": str(self.store_path),
            "MEGA_TRON_VERDICT_EMBEDDINGS": str(self.verdict_npz),
            # Block Gemini home-dir skill discovery: point HOME at an
            # empty sandbox so ~/.gemini/skills and ~/.agents/skills
            # resolve to empty dirs we control.
            "HOME": str(self.fake_home),
            "GEMINI_CLI_TRUST_WORKSPACE": "true",
        }
        if self.cond == "c0":
            env["MEGA_EVAL_BLEND"] = "0"
        return env


def _copy_skill_dir(src: Path, dst: Path) -> None:
    """Copy a SKILL.md directory into the workspace.

    Gemini's workspace-trust check rejects symlinks that resolve outside
    the workspace root (`Path not in workspace: ...`), so we materialize
    the fixture skills inside the sandbox. Verdict frontmatter writes
    land in the copies, not the source pool — original files stay clean.
    """
    import shutil
    if dst.exists() or dst.is_symlink():
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        else:
            shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=False)


def collect_pool_paths() -> list[Path]:
    """Return paths to all 80 SKILL.md *directories* (one per skill)."""
    out: list[Path] = []
    for tier in ("real", "poisoned", "competing", "noise"):
        for entry in sorted((FIXTURE_ROOT / "pool" / tier).iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").exists():
                out.append(entry.resolve())
    if len(out) != 80:
        print(f"[warn] expected 80 skills, found {len(out)}", file=sys.stderr)
    return out


def setup_sandbox(cond: str, pool_paths: list[Path]) -> Sandbox:
    base = SANDBOX_ROOT / cond
    workspace = (base / "workspace").resolve()
    skills_dir = workspace / ".gemini" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    # Materialize the 80-skill pool inside the workspace (Gemini's
    # workspace-trust check rejects symlinks that resolve outside).
    for src in pool_paths:
        _copy_skill_dir(src, skills_dir / src.name)
    # Trust the workspace so Gemini loads project settings
    settings_path = workspace / ".gemini" / "settings.json"
    settings_path.write_text(
        json.dumps({"hooks": {"BeforeAgent": [], "AfterAgent": []}}, indent=2) + "\n",
        encoding="utf-8",
    )
    trusted_path = workspace / ".gemini" / "trustedFolders.json"
    trusted_path.write_text(json.dumps({"trusted": True}, indent=2) + "\n", encoding="utf-8")
    store_path = (base / "store.db").resolve()
    verdict_npz = (base / "verdicts.npz").resolve()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    # Reset store/embedding artifacts so each run starts cold
    store_path.unlink(missing_ok=True)
    verdict_npz.unlink(missing_ok=True)
    # Fake HOME so Gemini's home-level skill discovery hits empty dirs.
    fake_home = (base / "fake_home").resolve()
    (fake_home / ".gemini" / "skills").mkdir(parents=True, exist_ok=True)
    (fake_home / ".agents" / "skills").mkdir(parents=True, exist_ok=True)
    return Sandbox(
        cond=cond,
        workspace=workspace,
        skills_dir=skills_dir,
        store_path=store_path,
        verdict_npz=verdict_npz,
        fake_home=fake_home,
    )


# ---------------------------------------------------------------------------
# MegaCore with isolated store
# ---------------------------------------------------------------------------


def build_core(sandbox: Sandbox):
    """Construct MegaCore pointing at the sandbox store + skill pool.

    Only the mega-tron-relevant env vars get applied to this process —
    HOME and GEMINI_CLI_TRUST_WORKSPACE stay in the subprocess env,
    so the parent process keeps using the real HuggingFace cache, etc.

    Embedder override: MegaCore's constructor reads ``config.toml`` and
    passes ``embedder_model`` explicitly to ``make_embedder``, which beats
    the ``MEGA_EMBEDDER_MODEL`` env var inside the factory. To let the
    experiment pick a different embedder than the user's daily default
    without mutating their config.toml, we explicitly build a
    ``Config`` here that prefers the env var if set.
    """
    _IN_PROCESS_KEYS = {"MEGA_TRON_STORE", "MEGA_TRON_VERDICT_EMBEDDINGS", "MEGA_EVAL_BLEND"}
    for k, v in sandbox.env_overrides.items():
        if k in _IN_PROCESS_KEYS:
            os.environ[k] = v
    # Force ranker to re-read weights (it reads at module-load time).
    from mega_tron import ranker as _ranker
    _ranker.reload_weights()

    from mega_tron.config import Config
    from mega_tron.core import MegaCore
    from mega_tron.verdicts.store import Store

    cfg = Config.load()
    env_model = os.environ.get("MEGA_EMBEDDER_MODEL", "").strip()
    if env_model:
        cfg.embedder_model = env_model
        print(f"[fb-loop] embedder pinned by env: {env_model}")

    store = Store(sandbox.store_path)
    store.initialize()
    core = MegaCore(skills_dirs=[sandbox.skills_dir], store=store, config=cfg)
    core.warmup_if_stale()
    # Confirm post-construction so the log row makes drift visible.
    print(f"[fb-loop] embedder active: {core._router.embedder.model_id}")
    return core


# ---------------------------------------------------------------------------
# Measurement — per-round breakdown logging
# ---------------------------------------------------------------------------


def record_measurement(core, prompts: list[dict], round_idx: int, sandbox: Sandbox, out_dir: Path) -> None:
    """For each prompt, route() top-10 and log rank + score per ranked skill.

    Score is the final post-blend value the router uses for ordering
    (raw cosine under C0 since MEGA_EVAL_BLEND=0; verdict-adjusted
    under C3). We capture the mega_meta state per ranked skill so
    analyze.py can reconstruct status changes (active/suspect/archived)
    and helpful/harmful_count totals over rounds without needing
    breakdown attached to RankedSkill itself.
    """
    out_path = out_dir / f"{sandbox.cond}_round_{round_idx}.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for p in prompts:
            ranked = core.route(p["prompt"], top_k=10)
            for rank_idx, r in enumerate(ranked, start=1):
                skill_obj = getattr(r, "skill", None)
                meta = getattr(skill_obj, "mega_meta", None) if skill_obj is not None else None
                row = {
                    "round": round_idx,
                    "cond": sandbox.cond,
                    "prompt_id": p["id"],
                    "expected_skill": p.get("expected_skill"),
                    "rank": rank_idx,
                    "skill_name": getattr(r, "name", None) or getattr(r, "skill_name", ""),
                    "score": float(getattr(r, "score", 0.0) or 0.0),
                }
                if meta is not None:
                    row["status"] = getattr(meta, "status", None)
                    row["helpful_count"] = getattr(meta, "helpful_count", 0)
                    row["harmful_count"] = getattr(meta, "harmful_count", 0)
                    row["consecutive_harmful"] = getattr(meta, "consecutive_harmful", 0)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Gemini call + verdict capture
# ---------------------------------------------------------------------------


# Tagging contract is the single source of truth in
# mega_tron.self_eval_contract. The experiment uses the same text the
# production prependers inject, so verdict noise measured here reflects
# real-world deployed contract behavior.
from mega_tron.self_eval_contract import render_inline_self_eval_contract  # noqa: E402

SELF_REPORT_CONTRACT = "\n\n---\n" + render_inline_self_eval_contract() + "\n"


def render_catalog(ranked) -> str:
    """Render router top-K as a Hermes-style mandatory catalog block."""
    lines = [
        "## Skills (mandatory)",
        "Before replying, scan the skills below. If a skill matches or is even partially relevant "
        "to your task, you MUST load it with skill_view(name) and follow its instructions. "
        "Err on the side of loading.",
        "",
        "<available_skills>",
    ]
    for r in ranked:
        name = getattr(r, "name", None) or getattr(r, "skill_name", "")
        skill_obj = getattr(r, "skill", None)
        desc = (
            getattr(r, "description", None)
            or getattr(skill_obj, "description", None)
            or ""
        )
        desc = " ".join(desc.split())[:300]
        if not name:
            continue
        if desc:
            lines.append(f"  - {name}: {desc}")
        else:
            lines.append(f"  - {name}")
    lines.append("</available_skills>")
    return "\n".join(lines)


def call_gemini(prompt_text: str, sandbox: Sandbox, timeout_s: int = TIMEOUT_S) -> dict:
    """Spawn Gemini in its own process group so we can hard-kill the
    entire tree on timeout. The previous version used ``subprocess.run``
    with a plain ``timeout=`` which only SIGTERMs the direct child —
    grandchildren (Node, model client) would leak and freeze ``run()``
    waiting on the pipe forever. We now ``Popen(preexec_fn=os.setsid)``
    and ``os.killpg(SIGKILL)`` the whole group when the deadline hits.
    """
    import signal
    env = os.environ.copy()
    # Sandbox env carries HOME override, isolated store path, and the
    # workspace-trust flag.
    env.update(sandbox.env_overrides)
    cmd = [
        GEMINI_BIN,
        "--prompt",
        prompt_text,
        "--model",
        GEMINI_MODEL,
        "--approval-mode",
        "plan",
        "--output-format",
        "stream-json",
    ]
    t0 = time.time()
    proc = subprocess.Popen(
        cmd,
        cwd=str(sandbox.workspace),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # equivalent to preexec_fn=os.setsid; gives us a pgid to killpg
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        return {
            "rc": proc.returncode,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "elapsed_s": time.time() - t0,
        }
    except subprocess.TimeoutExpired:
        # Kill the entire process group so any node/python grandchildren
        # die too. communicate() once more to drain pipes; bound by a
        # short wait so we never block on a dead group.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = "", ""
        return {
            "rc": -1,
            "stdout": stdout or "",
            "stderr": f"TIMEOUT after {timeout_s}s (group SIGKILLed)",
            "elapsed_s": time.time() - t0,
        }


def run_learning_turn(
    core,
    prompt: dict,
    round_idx: int,
    sandbox: Sandbox,
    out_path: Path,
) -> None:
    ranked = core.route(prompt["prompt"], top_k=5)
    catalog = render_catalog(ranked)
    full_prompt = catalog + SELF_REPORT_CONTRACT + "\n\nUser task:\n" + prompt["prompt"]
    res = call_gemini(full_prompt, sandbox)
    text = parse_stream_json(res["stdout"]).get("final_text", "") or res["stdout"]
    tags = _parse_skill_used_tags(text)
    # Persist verdicts (skipped on C0 wouldn't be honest — verdicts still
    # accumulate in the C0 store for symmetric comparison, but
    # MEGA_EVAL_BLEND=0 means they don't influence ranking. This way
    # any rank divergence between C3 and C0 must come from the blend
    # weights, not from one side having no data.)
    session_id = f"{sandbox.cond}-r{round_idx}-{prompt['id']}"
    if tags:
        try:
            persist_verdicts(
                skills_dir=sandbox.skills_dir,
                verdicts=[
                    {"skill": t["name"], "verdict": t["verdict"], "reason": t["reason"]}
                    for t in tags
                ],
                host="gemini",
                session_id=session_id,
                log_prefix=f"[fb-loop {sandbox.cond}]",
            )
        except Exception as e:
            print(f"[fb-loop {sandbox.cond}] persist_verdicts failed: {e}", file=sys.stderr)
    # Dump raw response + extracted text to per-turn files for offline
    # debugging (catalog vs. tag-emission, full reasoning traces).
    raw_dir = out_path.parent / "raw" / sandbox.cond
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"r{round_idx}_{prompt['id']}.stream.json").write_text(res["stdout"] or "", encoding="utf-8")
    (raw_dir / f"r{round_idx}_{prompt['id']}.text.md").write_text(text or "", encoding="utf-8")
    log_row = {
        "round": round_idx,
        "cond": sandbox.cond,
        "prompt_id": prompt["id"],
        "expected_skill": prompt.get("expected_skill"),
        "session_id": session_id,
        "elapsed_s": res["elapsed_s"],
        "rc": res["rc"],
        "ranked_top5": [getattr(r, "name", "") for r in ranked],
        "tags": tags,
        "text_len": len(text),
        "text_tail": (text or "")[-600:],
        "stderr_head": (res["stderr"] or "")[:300],
    }
    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(log_row, ensure_ascii=False) + "\n")
    print(
        f"  [{sandbox.cond} R{round_idx} {prompt['id']:20s}] elapsed={res['elapsed_s']:5.1f}s "
        f"tags={len(tags)} rc={res['rc']}"
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def load_prompts() -> list[dict]:
    import yaml
    fx = yaml.safe_load((FIXTURE_ROOT / "fixtures.yaml").read_text())
    prompts: list[dict] = []
    for row in fx.get("in_distribution", []):
        prompts.append({**row, "kind": "in_distribution"})
    for row in fx.get("null_prompts", []):
        prompts.append({**row, "kind": "null"})
    return prompts


def run_condition(cond: str, prompts: list[dict], rounds: int, out_dir: Path, pool_paths: list[Path], smoke_limit: int | None = None) -> None:
    print(f"\n=== condition {cond} ===")
    sandbox = setup_sandbox(cond, pool_paths)
    print(f"  workspace={sandbox.workspace}")
    print(f"  store={sandbox.store_path}")
    print(f"  npz={sandbox.verdict_npz}")
    core = build_core(sandbox)

    turn_log = out_dir / f"{cond}_turns.jsonl"
    if turn_log.exists():
        turn_log.unlink()

    fold = prompts if smoke_limit is None else prompts[:smoke_limit]
    for r in range(rounds):
        print(f"-- round {r}: measurement ({len(fold)} prompts)")
        record_measurement(core, fold, r, sandbox, out_dir)
        print(f"-- round {r}: learning turns ({len(fold)} prompts)")
        for p in fold:
            run_learning_turn(core, p, r, sandbox, turn_log)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=6, help="Number of rounds (R0..R{n-1}); default 6")
    parser.add_argument("--conditions", nargs="+", default=["c0", "c3"], choices=["c0", "c3"])
    parser.add_argument("--smoke", type=int, default=None, help="Smoke mode: only first N prompts per round")
    parser.add_argument(
        "--results-dir",
        default=None,
        help="Override results dir (default: results/<UTC-timestamp>)",
    )
    args = parser.parse_args()

    pool_paths = collect_pool_paths()
    prompts = load_prompts()
    print(f"[fb-loop] pool={len(pool_paths)} prompts={len(prompts)}")

    if args.results_dir:
        results_dir = Path(args.results_dir).resolve()
    else:
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        results_dir = (FIXTURE_ROOT / "results" / ts).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    print(f"[fb-loop] results -> {results_dir}")

    # If multiple conditions requested, fork a fresh child per condition.
    # Same-process re-init of the BGE embedder triggers a PyThread lock
    # deadlock on macOS (observed: 8h hang in PyThread_acquire_lock after
    # c0 finished and c3 tried to reload weights). A clean process per
    # condition sidesteps it without giving up the sandbox guarantees.
    if len(args.conditions) > 1:
        for cond in args.conditions:
            child_cmd = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--rounds", str(args.rounds),
                "--conditions", cond,
                "--results-dir", str(results_dir),
            ]
            if args.smoke is not None:
                child_cmd += ["--smoke", str(args.smoke)]
            print(f"\n[fb-loop] launching child for cond={cond}: {' '.join(child_cmd)}")
            child_env = os.environ.copy()
            # Belt-and-braces against torch thread-pool weirdness, even
            # though each child is fresh anyway.
            child_env.setdefault("OMP_NUM_THREADS", "1")
            child_env.setdefault("MKL_NUM_THREADS", "1")
            child_env.setdefault("TOKENIZERS_PARALLELISM", "false")
            rc = subprocess.call(child_cmd, env=child_env)
            if rc != 0:
                print(f"[fb-loop] child cond={cond} exited rc={rc}", file=sys.stderr)
                return rc
    else:
        run_condition(args.conditions[0], prompts, args.rounds, results_dir, pool_paths, smoke_limit=args.smoke)

    print(f"\n[fb-loop] done. raw logs at {results_dir}")
    print(f"[fb-loop] next: uv run python {Path(__file__).parent}/analyze.py {results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
