/* mega-tron dashboard — vanilla JS controller.

   UX model:
   - Main page = overview card + verdict list (skill-grouped by default).
   - Detail panes open in a *horizontal rail* on the right. Clicking
     into a sub-detail (e.g. a verdict from inside a skill pane) opens
     a NEW pane to the right of the existing one. The user closes any
     pane via × (it slides out, panes to its right stay).
   - Treemap = explicit "View all" button, not the stacked bar.
*/

"use strict";

// Live-refresh cadence. The Stop hook writes a verdict the moment a
// turn ends, so a 30s tick used to leave a noticeable gap between
// "model just answered" and "row showed up in the dashboard". A
// loopback HTTP poll on a single-tenant server is essentially free,
// so we tighten the interval to 5s. The visibilitychange listener in
// `bootstrap` pauses polling while the tab is hidden, and an active
// TEXTAREA/INPUT focus skips one tick to avoid stomping a user's
// in-progress edit.
const POLL_MS = 5000;
// Frontend host display list. "hermes" and "agents" are intentionally
// hidden from the UI — the backend still classifies hermes-rooted /
// ~/.agents-rooted skills correctly (hosts/__init__.py
// infer_host_from_skill_dir), but the dashboard surface only shows
// the three first-class CLIs plus user-recorded verdicts.
// "other" intentionally absent; "user" = manual verdicts.
const HOSTS = ["codex", "claude", "gemini", "user"];

function reportToServer(payload) {
  try {
    navigator.sendBeacon &&
      navigator.sendBeacon(
        "/api/debug/log",
        new Blob([JSON.stringify(payload)], { type: "application/json" }),
      );
  } catch (_e) { /* swallow */ }
}

window.addEventListener("error", (e) => {
  reportToServer({
    kind: "error",
    message: e.message || String(e.error || ""),
    filename: e.filename || "", line: e.lineno, col: e.colno,
    stack: e.error && e.error.stack ? String(e.error.stack) : null,
  });
});
window.addEventListener("unhandledrejection", (e) => {
  reportToServer({
    kind: "unhandledrejection",
    reason: String(e.reason),
    stack: e.reason && e.reason.stack ? String(e.reason.stack) : null,
  });
});

const state = {
  // Active top-level tab. Three tabs:
  //  - "context-savings" — landing for fresh installs. Token-injection
  //    estimate against the user's real catalog vs vanilla hosts. This
  //    is the headline value proposition of mega-tron, so it's the
  //    default tab; existing localStorage values are honored.
  //  - "overview" — catalog observability (big numbers / host bars /
  //    active-skills list).
  //  - "review" — HITL verdict cleanup (verdicts list, skill detail
  //    pane, orphan pane).
  // Persisted to localStorage so a reload keeps the user on whichever
  // tab they were last using.
  activeTab: (typeof localStorage !== "undefined"
    && localStorage.getItem("megaTronTab")) || "context-savings",
  // Time window applied to /api/overview, /api/skills{,-by-name},
  // /api/verdicts via qsParams(). Fixed at 30d because the
  // user-facing toggle that used to sit under Activity was removed
  // along with the Activity card; the constant kept its name so the
  // rest of the code doesn't need to learn a new shape.
  timeRange: 30,
  hostFilter: null,
  listMode: "skills", // "skills" | "verdicts"
  // Panes is an array of { id, kind, payload }. Each renders as one
  // column in the right-side rail. Open is "append to right"; close
  // removes by id and leaves the rest visible.
  panes: [],
  paneSeq: 1,
  overview: null,
  skillsByName: [],
  skills: [],
  verdicts: [],
  // Tab 0 (Context savings) payload from /api/context-savings.
  // Static estimate, recomputed on every poll because the user might
  // install/remove skills between turns.
  contextSavings: null,
  // Multi-category search for the Review tab. Replaces the old
  // verdict-reason FTS search. `field` decides which value the
  // string match runs against; `value` is the query string (or a
  // host short-name when field === "hosts").
  reviewFilter: { field: "title", value: "" },
  // Zero-based page index for each paged list. Reset to 0 whenever
  // the underlying filter or sort key changes.
  pages: { active: 0, helpful: 0, skillsReview: 0, verdictsReview: 0 },
};

// Items-per-page caps. Two lists in tab 1 share a tighter cap so
// both fit above the fold; tab 2's verdicts/skills lists carry
// more density per row so 20 reads better.
const PAGE_SIZE_TAB1 = 10;
const PAGE_SIZE_TAB2 = 20;

let pollTimer = null;
let loadInFlight = false;

// ---------- Fetch helpers ---------- //

function qsParams() {
  const p = new URLSearchParams();
  if (state.timeRange > 0) p.set("days", String(state.timeRange));
  if (state.hostFilter) p.set("host", state.hostFilter);
  return p;
}

async function fetchJSON(path, signal) {
  const resp = await fetch(path, { signal, headers: { Accept: "application/json" } });
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  return resp.json();
}

async function loadAll() {
  if (loadInFlight) return;
  loadInFlight = true;
  const params = qsParams();
  // Filter is applied client-side now (title/description/hosts
  // substring match against skill rows or verdict rows); no need
  // to hit the server-side /api/verdicts/search FTS endpoint here.
  try {
    const [overview, skillsByName, skills, verdicts, contextSavings] = await Promise.all([
      fetchJSON(`/api/overview?${params}`),
      fetchJSON(`/api/skills-by-name?${params}`),
      fetchJSON(`/api/skills?${params}`),
      fetchJSON(`/api/verdicts?limit=100&${params}`),
      fetchJSON(`/api/context-savings`),
    ]);
    state.overview = overview;
    state.skillsByName = skillsByName;
    state.skills = skills;
    state.verdicts = verdicts;
    state.contextSavings = contextSavings;
    setConnection("live");
    renderAll();
  } catch (err) {
    if (err.name !== "AbortError") {
      setConnection("stale");
      console.error("dashboard load failed", err);
    }
  } finally {
    loadInFlight = false;
  }
}

function setConnection(status) {
  const el = document.getElementById("connection");
  if (!el) return;
  el.classList.remove("live", "stale");
  el.classList.add(status);
  el.textContent = status === "live" ? "live" : "connection lost";
}

// ---------- Rendering ---------- //

function renderAll() {
  if (state.activeTab === "context-savings") {
    renderContextSavings();
  } else if (state.activeTab === "overview") {
    renderBigNumbers();
    renderHostBars();
    renderActiveSkillsList();
  } else {
    renderHealth();
    renderMainList();
  }
  // Pull every server-side spinner placeholder once data has landed.
  // Done globally (not per-tab) so switching tabs after first paint
  // always lands on populated content, never a stale "Loading…" card.
  // Context savings has its own root-replace render path and is
  // already covered there — this only clears the overview/review
  // placeholders.
  document.querySelectorAll("[data-tab-placeholder]").forEach((el) => {
    el.remove();
  });
}

// ---------- Tab switching ---------- //

function setActiveTab(name) {
  if (name !== "context-savings" && name !== "overview" && name !== "review") return;
  if (state.activeTab === name) return;
  state.activeTab = name;
  try {
    localStorage.setItem("megaTronTab", name);
  } catch (_e) {
    // localStorage may be disabled in incognito; failure is harmless.
  }
  syncTabChrome();
  renderAll();
}

function syncTabChrome() {
  document.querySelectorAll("[data-tab]").forEach((b) => {
    const on = b.dataset.tab === state.activeTab;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll("[data-tab-panel]").forEach((p) => {
    p.hidden = p.dataset.tabPanel !== state.activeTab;
  });
}

// ---------- Tab 0 — Context savings (token-injection estimate) ---------- //

// Each host's native-catalog doc, plus a short rule summary used in the
// info tooltip. `docPath` is rendered as a GitHub repo link so the user
// can click through to the full analysis we keep in `docs/`. The same
// repo URL is used for every host — only the path under it varies.
const CS_DOCS_REPO = "https://github.com/mega-edo/mega-tron/blob/main";
const CS_HOSTS = {
  codex: {
    label: "codex",
    docPath: "docs/Native%20Skill%20Catalog%20in%20Codex%20CLI.md",
    blurb:
      "Codex ships a char-budget catalog every turn: min(2% × ctx, 8K chars). " +
      "Skills listed alphabetically by name; names always come first, descriptions " +
      "filled in round-robin until budget runs out. Past the cap, names get dropped " +
      "entirely.",
  },
  claude: {
    label: "claude",
    docPath: "docs/Native%20Skill%20Catalog%20in%20Claude%20Code.md",
    blurb:
      "Claude's native catalog runs on a 1% × ctx token budget (~2K tokens). " +
      "Every skill name is ALWAYS emitted — descriptions are evicted LRU under " +
      "budget pressure. With a large catalog the model sees mostly bare names " +
      "with no trigger text, which collapses retrieval on close neighbours.",
  },
  gemini: {
    label: "gemini",
    docPath: "docs/Native%20Skill%20Catalog%20in%20Gemini%20CLI.md",
    blurb:
      "Gemini has no catalog cap. Every enabled skill ships its full name + " +
      "description + location as an XML block, every turn. Cost scales linearly " +
      "with pool size (~99 tok per skill including XML framing).",
  },
  megatron: {
    label: "mega-tron",
    docPath: "docs/mega-tron%20routing.md",
    blurb:
      "Per turn: embed the user's prompt, cosine-rank every skill in the pool, " +
      "ship only the top-K above the gap-cut threshold. Pool size becomes " +
      "irrelevant — token cost is proportional to RELEVANCE, not to how many " +
      "skills you've installed.",
  },
};

// Per-host advice icon — only rendered when the endpoint attaches
// rule_advice + rule_severity to the row. severity drives the glyph:
//   "ok"      → ✓ (info/success, no immediate action needed)
//   "suggest" → 💡 (an upgrade exists and is worth considering)
//   "warn"    → ⚠ (current settings are wasting tokens — fix recommended)
// The full advice text shows on hover via the existing
// .tooltip-trigger[data-tooltip]::after CSS (instant, no native delay).
function _csAdviceIconHTML(host) {
  const advice = host && host.rule_advice;
  if (!advice) return "";
  const sev = host.rule_severity || "info";
  const glyph = sev === "ok" ? "✓" : sev === "warn" ? "⚠" : "💡";
  const safe = String(advice).replace(/"/g, "&quot;");
  return ` <span class="cs-advice-icon sev-${sev} tooltip-trigger" tabindex="0"
                 role="img" aria-label="${safe}"
                 data-tooltip="${safe}">${glyph}</span>`;
}

function _csInfoIconHTML(blurb, docPath) {
  // Single inline pattern reused for every info icon on this tab.
  // Click-to-open-docs + hover-to-read-blurb.
  const safeBlurb = blurb.replace(/"/g, "&quot;");
  const docURL = `${CS_DOCS_REPO}/${docPath}`;
  // Two children inside the wrapper:
  //   1. an instant-tooltip span (hover shows blurb)
  //   2. an external-link anchor (click jumps to the docs page)
  return `<span class="cs-info-pair">
    <span class="info-icon tooltip-trigger" tabindex="0" role="img"
          aria-label="${safeBlurb}" data-tooltip="${safeBlurb}">ⓘ</span>
    <a class="cs-doc-link" href="${docURL}" target="_blank" rel="noopener noreferrer"
       title="Open native-catalog analysis in the repo">docs ↗</a>
  </span>`;
}

function _csHostInstalledCountPhrase(n) {
  if (n === 1) return "Per turn across your 1 installed host";
  return `Per turn across your ${n} installed hosts`;
}

// mega-tron row badge — switches between "warming up · 7/20" and
// "measured · 47 sessions" based on how many route rows the store has.
// Below the warm-up threshold the median is too noisy to trust, so
// the UI labels it as in-progress while still showing the reference
// value next to it. A "session" = one mega-tron hook fire (the
// host's first prompt of a session). Subsequent turns in the same
// session reuse the already-injected catalog and don't re-rank, so
// they're not separate measurement points.
function _csMegaTronBadge(cs) {
  if (cs.mega_tron_is_measured) {
    const n = cs.mega_tron_turn_count || 0;
    return `<span class="cs-multiplier">measured · ${n} sessions</span>`;
  }
  const n = cs.mega_tron_turn_count || 0;
  const t = cs.warm_up_threshold || 20;
  return `<span class="cs-warming">warming up · ${n}/${t} sessions</span>`;
}

function renderContextSavings() {
  const root = document.getElementById("context-savings-root");
  if (!root) return;
  const cs = state.contextSavings;
  if (!cs) {
    // Mirror the static placeholder so an in-flight re-render (e.g.
    // navigation back to the tab while the poll is still pending)
    // doesn't strip the spinner.
    root.innerHTML = `
      <section class="card cs-loading">
        <span class="cs-spinner" aria-hidden="true"></span>
        <span class="cs-loading-text">Scanning your catalogs and simulating native injection costs…</span>
      </section>
    `;
    return;
  }

  const perHost = cs.per_host || {};
  const installed = Object.keys(perHost);
  if (installed.length === 0) {
    root.innerHTML = `
      <section class="card cs-empty">
        <div class="card-title">No skills detected</div>
        <div class="cs-empty-body">
          No skills under <code>~/.codex/skills</code>,
          <code>~/.claude/skills</code>, <code>~/.gemini/skills</code>,
          or the shared <code>~/.agents/skills</code>.
          <br><br>
          Run <code>mega-tron setup</code> to wire up your installed hosts,
          then this tab will fill in.
        </div>
      </section>
    `;
    return;
  }

  const vanillaSum = cs.vanilla_sum_tokens_per_turn || 0;
  const megaTron = cs.mega_tron_per_turn || 0;
  const baselineSource = cs.mega_tron_baseline_source || "";
  const multiplier = cs.multiplier || 0;
  const sharedSkillCount = cs.shared_skill_count || 0;
  // Axis max for the hero comparison bars.
  const heroAxis = Math.max(vanillaSum, megaTron, 1);
  const vanillaWidth = (vanillaSum / heroAxis) * 100;
  const megaWidth = Math.max((megaTron / heroAxis) * 100, 0.6); // floor so the bar is at least visible

  // Per-host bar axis = max single-host tokens (so the bars compare
  // honestly with each other, not with the sum).
  let perHostAxis = megaTron;
  for (const h of Object.keys(perHost)) {
    perHostAxis = Math.max(perHostAxis, perHost[h].tokens_per_turn || 0);
  }
  perHostAxis = Math.max(perHostAxis, 1);

  const heroTooltip = (
    cs.mega_tron_is_measured
      ? (
        "Vanilla: each installed host's native catalog at your current " +
        "skill count, summed (every host treats ~/.agents/skills as part " +
        "of its catalog too). mega-tron: your own sample median over the " +
        "last 30 days of sessions. Catalog overhead only — doesn't count " +
        "your prompt itself."
      )
      : (
        "Vanilla: each installed host's native catalog at your current " +
        "skill count, summed (every host treats ~/.agents/skills as part " +
        "of its catalog too). mega-tron: reference value (~150 tok/session " +
        "from the published benchmark) — switches to your own sample median " +
        "after you log " + (cs.warm_up_threshold || 20) + " sessions. " +
        "Each session = one mega-tron hook fire (the host's first prompt of " +
        "the session)."
      )
  );

  const heroHTML = `
    <section class="card cs-hero">
      <div class="card-title">
        <span class="title-with-info">
          <span>${_csHostInstalledCountPhrase(installed.length)}</span>
          <span class="info-icon tooltip-trigger" tabindex="0" role="img"
                aria-label="${heroTooltip.replace(/"/g, '&quot;')}"
                data-tooltip="${heroTooltip.replace(/"/g, '&quot;')}">ⓘ</span>
        </span>
      </div>
      <div class="cs-hero-bars">
        <div class="cs-hero-row">
          <span class="cs-hero-label">vanilla</span>
          <span class="cs-hero-track">
            <span class="cs-hero-fill vanilla" style="width:${vanillaWidth}%"></span>
          </span>
          <span class="cs-hero-tok">${vanillaSum.toLocaleString()} tok</span>
        </div>
        <div class="cs-hero-row">
          <span class="cs-hero-label">mega-tron</span>
          <span class="cs-hero-track">
            <span class="cs-hero-fill mega" style="width:${megaWidth}%"></span>
          </span>
          <span class="cs-hero-tok">${megaTron.toLocaleString()} tok</span>
        </div>
      </div>
      <div class="cs-hero-takeaway">
        ${multiplier > 0
          ? `mega-tron ships <strong>${multiplier.toLocaleString()}× fewer tokens</strong> per session ${
              cs.mega_tron_is_measured
                ? `<span class="cs-takeaway-note">(sample median over ${cs.mega_tron_turn_count} sessions)</span>`
                : `<span class="cs-takeaway-note">(reference value — switches to your sample median after ${cs.warm_up_threshold || 20} sessions)</span>`
            }.`
          : `mega-tron reference ~${megaTron} tok/session.`
        }
      </div>
    </section>
  `;

  // --- Per-host comparison bars ---
  const hostRows = [];
  for (const host of Object.keys(CS_HOSTS)) {
    if (host === "megatron") continue; // baseline row is appended below
    const h = perHost[host];
    if (!h) continue;
    const tok = h.tokens_per_turn || 0;
    const width = (tok / perHostAxis) * 100;
    const ratio = tok > 0 && megaTron > 0
      ? Math.max(1, Math.floor(tok / megaTron)) : 0;
    let label = CS_HOSTS[host].label;
    if (host === "claude" && h.claude_mode) {
      label = `claude (${h.claude_mode})`;
    }
    hostRows.push(`
      <div class="cs-host-row host-${host}">
        <div class="cs-host-row-top">
          <span class="cs-host-label-cell">
            <span class="cs-host-label">${label}</span>
            ${_csInfoIconHTML(CS_HOSTS[host].blurb, CS_HOSTS[host].docPath)}
          </span>
          <span class="cs-host-bar-track">
            <span class="cs-host-bar-fill ${host}" style="width:${width}%"></span>
          </span>
          <span class="cs-host-tok">${tok.toLocaleString()} tok</span>
          ${ratio > 0
            ? `<span class="cs-multiplier">${ratio.toLocaleString()}× mega-tron</span>`
            : ""
          }
        </div>
        <div class="cs-host-row-sub">${h.rule_summary || ""}${_csAdviceIconHTML(h)}</div>
      </div>
    `);
  }
  // mega-tron baseline row (always last).
  const megaRowWidth = Math.max((megaTron / perHostAxis) * 100, 0.6);
  hostRows.push(`
    <div class="cs-host-row host-megatron">
      <div class="cs-host-row-top">
        <span class="cs-host-label-cell">
          <span class="cs-host-label">mega-tron</span>
          ${_csInfoIconHTML(CS_HOSTS.megatron.blurb, CS_HOSTS.megatron.docPath)}
        </span>
        <span class="cs-host-bar-track">
          <span class="cs-host-bar-fill mega" style="width:${megaRowWidth}%"></span>
        </span>
        <span class="cs-host-tok">${megaTron.toLocaleString()} tok</span>
        ${_csMegaTronBadge(cs)}
      </div>
      <div class="cs-host-row-sub">${baselineSource}</div>
    </div>
  `);

  const perHostHTML = `
    <section class="card cs-perhost">
      <div class="card-title">Per-host comparison</div>
      <div class="card-sub">Static estimate based on each host's native injection rules at your current catalog size.</div>
      <div class="cs-host-list">${hostRows.join("")}</div>
    </section>
  `;

  // --- Catalog breakdown table ---
  // One row per host. Five compact columns the user can scan side-by-side:
  //   HOST | SEES | YOUR FILES | SHARED OVERLAP | UNIQUE
  // The shared pool gets a footer row with its own count + a "+ pulled
  // into every host above" annotation. Directory paths move to a small
  // footnote under the table so they're available without crowding the
  // numerical comparison.
  const tableRows = [];
  const dirRows = [];
  for (const host of Object.keys(CS_HOSTS)) {
    if (host === "megatron") continue;
    const h = perHost[host];
    if (!h) continue;
    const privateCount = h.private_skill_count || 0;
    const overlap = h.overlap_with_shared || 0;
    const unique = h.unique_to_host || 0;
    const visible = h.skill_count || 0;
    const pctOverlap = privateCount > 0 ? Math.round((overlap / privateCount) * 100) : 0;
    const overlapCell = overlap > 0
      ? `${overlap.toLocaleString()} <span class="cs-tbl-pct">(${pctOverlap}%)</span>`
      : `<span class="cs-tbl-muted">0</span>`;
    const uniqueCell = unique > 0
      ? unique.toLocaleString()
      : `<span class="cs-tbl-muted">0</span>`;
    tableRows.push(`
      <tr class="cs-tbl-row host-${host}">
        <td class="cs-tbl-host"><span class="cs-tbl-host-label">${CS_HOSTS[host].label}</span></td>
        <td class="cs-tbl-num">${visible.toLocaleString()}</td>
        <td class="cs-tbl-num">${privateCount.toLocaleString()}</td>
        <td class="cs-tbl-num">${overlapCell}</td>
        <td class="cs-tbl-num">${uniqueCell}</td>
      </tr>
    `);
    dirRows.push(
      `<span class="cs-dir-entry"><span class="cs-dir-tag host-${host}">${CS_HOSTS[host].label}</span> <code>${h.skills_dir}</code></span>`
    );
  }
  // Shared footer row: visually offset so the user knows it's the source
  // the per-host rows pull from. SEES and SHARED-OVERLAP are self-
  // referential for the shared pool (it IS the shared source — comparing
  // it to itself is meaningless), so we dash them out. UNIQUE here uses
  // the SAME definition as the other rows ("only here, nowhere else"):
  // names that live in ~/.agents/skills but in none of the host private
  // dirs. The full shared count is in YOUR FILES.
  const sharedOnly = cs.shared_only_count ?? 0;
  const sharedFooter = sharedSkillCount > 0
    ? `<tr class="cs-tbl-row shared-row">
         <td class="cs-tbl-host"><span class="cs-tbl-host-label">shared</span></td>
         <td class="cs-tbl-num"><span class="cs-tbl-muted">—</span></td>
         <td class="cs-tbl-num">${sharedSkillCount.toLocaleString()}</td>
         <td class="cs-tbl-num"><span class="cs-tbl-muted">—</span></td>
         <td class="cs-tbl-num">${sharedOnly.toLocaleString()}</td>
       </tr>`
    : "";

  // Grand-total row: union of every name across every dir, counted
  // once. Surfaced as a dedicated bottom row so it can't be confused
  // with the per-row UNIQUE definition above.
  const totalUnique = cs.total_unique_count ?? 0;
  const totalRow = totalUnique > 0
    ? `<tr class="cs-tbl-row total-row">
         <td class="cs-tbl-host"><span class="cs-tbl-host-label">total</span></td>
         <td colspan="3" class="cs-tbl-total-note">distinct skills across every dir (union, deduped by name)</td>
         <td class="cs-tbl-num"><strong>${totalUnique.toLocaleString()}</strong></td>
       </tr>`
    : "";
  if (sharedSkillCount > 0) {
    dirRows.push(
      `<span class="cs-dir-entry"><span class="cs-dir-tag host-shared">shared</span> <code>${cs.shared_skills_dir || "~/.agents/skills"}</code></span>`
    );
  }

  const breakdownHTML = `
    <section class="card cs-breakdown">
      <div class="card-title">Your catalog right now</div>
      <div class="card-sub">${
        sharedSkillCount > 0
          ? `<code>~/.agents/skills</code> is host-neutral — every host sees those <strong>${sharedSkillCount.toLocaleString()}</strong> shared skills on top of its own folder.`
          : "Each row shows what that host sees in isolation."
      }</div>
      <table class="cs-tbl">
        <thead>
          <tr>
            <th class="cs-tbl-host">HOST</th>
            <th class="cs-tbl-num" title="Total skills this host actually sees (private ∪ shared, deduped)">SEES</th>
            <th class="cs-tbl-num" title="SKILL.md files under this host's own directory">YOUR FILES</th>
            <th class="cs-tbl-num" title="Of YOUR FILES, how many names also exist in ~/.agents/skills">SHARED OVERLAP</th>
            <th class="cs-tbl-num" title="Of YOUR FILES, how many are truly only in this host's directory">UNIQUE</th>
          </tr>
        </thead>
        <tbody>${tableRows.join("")}${sharedFooter}${totalRow}</tbody>
      </table>
      <div class="cs-dir-list">
        ${dirRows.join(" · ")}
      </div>
    </section>
  `;

  root.innerHTML = heroHTML + perHostHTML + breakdownHTML;
}

// ---------- Tab 1 — Skills overview (observability) ---------- //

function renderBigNumbers() {
  const wrap = document.getElementById("big-numbers");
  if (!wrap) return;
  if (!state.overview) {
    wrap.innerHTML = "";
    return;
  }
  const ov = state.overview;
  const total = ov.total || 0;
  const used = ov.used || 0;

  // Catalog empty — honest empty state instead of a row of zeros.
  if (total === 0) {
    wrap.innerHTML = `
      <div class="big-card big-card-empty">
        <div class="big-card-title">No skills detected</div>
        <div class="big-card-sub">
          Run <code>mega-tron setup</code> to wire up your hosts and
          start collecting routing signal.
        </div>
      </div>`;
    return;
  }

  // Count only hosts the frontend actually surfaces, so the
  // "across N hosts" line stays consistent with the host-bar chart
  // below (hermes is intentionally hidden in the UI).
  const hostsActive = HOSTS.filter((h) => {
    const raw = ov.by_host?.[h];
    const c = typeof raw === "number"
      ? raw : (raw?.used ?? raw?.total ?? 0);
    return c > 0;
  }).length;
  const pctUsed = (used / total) * 100;
  const pctLabel = pctUsed > 0 && pctUsed < 1
    ? `${pctUsed.toFixed(2)}%`
    : `${Math.round(pctUsed)}%`;

  // Verdict count for the 30-day window — sum across skill rows the
  // server already filtered with ?days=30. `state.skillsByName` may
  // still be loading on the very first paint; fall back to em-dash.
  const verdicts30d = Array.isArray(state.skillsByName)
    ? state.skillsByName.reduce((acc, r) => {
        return acc + (r.helpful_count || 0)
          + (r.harmful_count || 0) + (r.neutral_count || 0);
      }, 0)
    : null;

  const healthIssues = (ov.net_harmful_count || 0)
    + (ov.noise_verdict_count || 0)
    + (ov.orphan_count || 0)
    + (ov.unknown_host_count || 0);

  const healthBody = healthIssues === 0
    ? `<div class="big-card-value health-ok">✓ all clear</div>
       <div class="big-card-sub">no orphans, no noise, no net-harmful skills</div>`
    : `<div class="big-card-value health-warn">⚠ ${healthIssues}
         issue${healthIssues === 1 ? "" : "s"}</div>
       <div class="big-card-sub">
         <a href="#" class="big-card-link" data-act="go-review">
           open Review →
         </a>
       </div>`;

  wrap.innerHTML = `
    <div class="big-card">
      <div class="big-card-title">Total skills</div>
      <div class="big-card-value">${total.toLocaleString()}</div>
      <div class="big-card-sub">across ${hostsActive} host${hostsActive === 1 ? "" : "s"} with verdicts</div>
    </div>
    <div class="big-card">
      <div class="big-card-title">Used in 30d</div>
      <div class="big-card-value">${used.toLocaleString()}</div>
      <div class="big-card-sub">${pctLabel} of catalog</div>
    </div>
    <div class="big-card">
      <div class="big-card-title">Verdicts</div>
      <div class="big-card-value">${verdicts30d === null
        ? "—" : verdicts30d.toLocaleString()}</div>
      <div class="big-card-sub">last 30 days</div>
    </div>
    <div class="big-card">
      <div class="big-card-title">Health</div>
      ${healthBody}
    </div>`;

  // Wire the cross-tab link.
  const goReview = wrap.querySelector('[data-act="go-review"]');
  if (goReview) {
    goReview.addEventListener("click", (e) => {
      e.preventDefault();
      setActiveTab("review");
    });
  }
}

function renderHostBars() {
  const wrap = document.getElementById("host-bars");
  if (!wrap) return;
  if (!state.overview) {
    wrap.innerHTML = "";
    return;
  }
  // Normalise by_host into {host: count}; tolerate legacy {used,total}
  // shape the same way renderHostChips used to.
  const counts = {};
  for (const host of HOSTS) {
    const raw = state.overview.by_host?.[host];
    counts[host] = typeof raw === "number"
      ? raw
      : (raw && typeof raw === "object" ? (raw.used ?? raw.total ?? 0) : 0);
  }
  const max = Math.max(1, ...Object.values(counts));
  wrap.innerHTML = "";
  for (const host of HOSTS) {
    const count = counts[host];
    const row = document.createElement("button");
    row.type = "button";
    row.className = `host-bar-row ${host}`;
    if (count === 0) row.classList.add("empty");
    if (state.hostFilter === host) row.classList.add("active");
    const widthPct = max > 0 ? (count / max) * 100 : 0;
    row.innerHTML = `
      <span class="host-bar-label">${host}</span>
      <span class="host-bar-track">
        <span class="host-bar-fill" style="width:${widthPct}%"></span>
      </span>
      <span class="host-bar-count">${count}</span>
    `;
    row.addEventListener("click", () => toggleHost(host));
    wrap.appendChild(row);
  }
}

function renderActiveSkillsList() {
  // Two side-by-side lists in tab 1:
  //   - "Active skills" — used in last 30d, sorted by recency
  //   - "Most helpful skills" — used at all, sorted by net desc
  // Both feed off the same skillsByName corpus loadAll() already
  // fetched, so this is two client-side sorts and slices.
  const skillsByName = state.skillsByName || [];

  const cutoff = Date.now() - 30 * 24 * 60 * 60 * 1000;
  let active = skillsByName.filter((r) => {
    if (!r.last_updated) return false;
    const t = Date.parse(r.last_updated);
    return Number.isFinite(t) && t >= cutoff;
  });
  if (state.hostFilter) {
    active = active.filter((r) =>
      (r.hosts_seen || []).includes(state.hostFilter)
      || (r.installed_hosts || []).includes(state.hostFilter)
    );
  }
  active.sort((a, b) => Date.parse(b.last_updated) - Date.parse(a.last_updated));
  active = active.slice(0, 20);

  let helpful = skillsByName.filter((r) => {
    const h = r.helpful_count || 0;
    const x = r.harmful_count || 0;
    return h > 0 || x > 0; // any verdict at all
  });
  if (state.hostFilter) {
    helpful = helpful.filter((r) =>
      (r.hosts_seen || []).includes(state.hostFilter)
      || (r.installed_hosts || []).includes(state.hostFilter)
    );
  }
  helpful.sort((a, b) => {
    const netA = (a.net == null) ? (a.helpful_count || 0) - (a.harmful_count || 0) : a.net;
    const netB = (b.net == null) ? (b.helpful_count || 0) - (b.harmful_count || 0) : b.net;
    if (netA !== netB) return netB - netA;
    // tiebreaker: most helpful count first, then alphabetical.
    if ((b.helpful_count || 0) !== (a.helpful_count || 0)) {
      return (b.helpful_count || 0) - (a.helpful_count || 0);
    }
    return a.name.localeCompare(b.name);
  });
  helpful = helpful.slice(0, 20);

  _renderSkillsList({
    listId: "active-skills-list",
    titleId: "active-skills-title",
    pagerId: "active-skills-pager",
    pageKey: "active",
    pageSize: PAGE_SIZE_TAB1,
    titleLabel: "Active skills",
    rows: active,
    showTime: true,
    emptyText: state.hostFilter
      ? `No ${state.hostFilter} skills active in the last 30 days.`
      : "No skills active in the last 30 days. Verdicts you see this week will show up here.",
  });
  _renderSkillsList({
    listId: "helpful-skills-list",
    titleId: "helpful-skills-title",
    pagerId: "helpful-skills-pager",
    pageKey: "helpful",
    pageSize: PAGE_SIZE_TAB1,
    titleLabel: "Most helpful skills",
    rows: helpful,
    showTime: false,
    emptyText: state.hostFilter
      ? `No ${state.hostFilter} skills with verdicts yet.`
      : "No verdicts yet. Once mega-tron records HELPFUL / HARMFUL signal you'll see the top performers here.",
  });
}

// Internal: shared renderer for the two tab-1 skill lists.
// Handles paging via `state.pages[pageKey]` + a pager footer rendered
// into `#${pagerId}` when there's more than one page.
function _renderSkillsList({ listId, titleId, pagerId, pageKey, pageSize,
                            titleLabel, rows, showTime, emptyText }) {
  const list = document.getElementById(listId);
  const title = document.getElementById(titleId);
  if (!list) return;

  const total = rows.length;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  // Clamp the stored page in case rows shrank (filter changed, verdicts
  // were deleted, etc.) since the last render.
  let page = state.pages[pageKey] || 0;
  if (page >= pageCount) page = pageCount - 1;
  if (page < 0) page = 0;
  state.pages[pageKey] = page;

  if (title) title.textContent = `${titleLabel} (${total})`;
  list.innerHTML = "";
  if (total === 0) {
    const empty = document.createElement("li");
    empty.className = "active-empty";
    empty.textContent = emptyText;
    list.appendChild(empty);
    _renderPager(pagerId, { page: 0, pageCount: 1, total: 0,
      onChange: () => {} });
    return;
  }
  const start = page * pageSize;
  const slice = rows.slice(start, start + pageSize);
  for (const r of slice) {
    list.appendChild(_renderSkillRow(r, { showTime }));
  }
  _renderPager(pagerId, {
    page,
    pageCount,
    total,
    onChange: (next) => {
      state.pages[pageKey] = next;
      renderAll();
    },
  });
}

// Internal: renders Prev/Next pager into `#${pagerId}`. Hidden when
// there's only one page so single-page lists don't carry extra
// chrome.
function _renderPager(pagerId, { page, pageCount, total, onChange }) {
  const wrap = document.getElementById(pagerId);
  if (!wrap) return;
  if (pageCount <= 1) {
    wrap.innerHTML = "";
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  const from = page * Math.ceil(total / pageCount) + 1;
  // We compute "from / to" off the actual page-size used by the
  // caller via the slice; recompute here without that knowledge by
  // dividing total by page count to recover the per-page size. This
  // is exact when total > pageSize because Math.ceil(total / size)
  // === pageCount.
  const pageSize = Math.ceil(total / pageCount);
  const fromIdx = page * pageSize + 1;
  const toIdx = Math.min(total, (page + 1) * pageSize);
  wrap.innerHTML = `
    <button class="pager-btn" data-act="prev" ${page === 0 ? "disabled" : ""}>← Prev</button>
    <span class="pager-status">${fromIdx}–${toIdx} of ${total}</span>
    <button class="pager-btn" data-act="next" ${page >= pageCount - 1 ? "disabled" : ""}>Next →</button>
  `;
  wrap.querySelector('[data-act="prev"]').addEventListener("click", () => {
    if (page > 0) onChange(page - 1);
  });
  wrap.querySelector('[data-act="next"]').addEventListener("click", () => {
    if (page < pageCount - 1) onChange(page + 1);
  });
}

function _renderSkillRow(r, { showTime } = {}) {
  const li = document.createElement("li");
  li.className = showTime ? "active-row" : "active-row no-time";
  const helpful = r.helpful_count || 0;
  const harmful = r.harmful_count || 0;
  const net = (r.net == null) ? helpful - harmful : r.net;
  const netCls = net > 0 ? "pos" : (net < 0 ? "neg" : "");
  const hostsSeen = (r.hosts_seen || []).filter((h) => h !== "other");
  const primaryHost = hostsSeen[0] || "—";
  // Warning sign on net-harmful skills (harmful ≥ helpful with ≥1
  // harmful verdict). Custom CSS tooltip (`data-tooltip`) renders
  // instantly on hover — native `title` would lag ~600ms (browser
  // default) which fights observability ergonomics. `aria-label`
  // mirrors the message for screen readers.
  const harmTip = harmful > 0 && harmful >= helpful
    ? `${harmful} HARMFUL vs ${helpful} HELPFUL — this skill has earned more bad signal than good. Click to review.`
    : "";
  const harmFlag = harmful > 0 && harmful >= helpful
    ? `<span class="active-flag tooltip-trigger" tabindex="0" role="img"
              aria-label="${escapeAttr(harmTip)}"
              data-tooltip="${escapeAttr(harmTip)}">⚠</span>`
    : "";
  const timeCol = showTime
    ? `<span class="active-time">${relTime(r.last_updated)}</span>`
    : "";
  li.innerHTML = `
    <span class="active-name">${escapeHtml(r.name)}</span>
    <span class="active-host">${escapeHtml(primaryHost)}</span>
    <span class="active-counts">
      <span class="count-pill HELPFUL" title="helpful">${helpful}</span>
      <span class="count-pill HARMFUL" title="harmful">${harmful}</span>
    </span>
    <span class="active-net ${netCls}" title="net = helpful − harmful">${net > 0 ? "+" : ""}${net}</span>
    ${timeCol}
    ${harmFlag}
  `;
  // Click → jump to Review tab and open this skill's detail pane.
  // The detail pane shows every verdict (helpful / harmful / neutral)
  // for the skill, which is the "go review this skill's verdicts"
  // intent. Opening it directly is cleaner than wiring a separate
  // verdicts-list filter that the user would then have to clear.
  li.addEventListener("click", () => jumpToSkillReview(r.name));
  return li;
}

function jumpToSkillReview(name) {
  setActiveTab("review");
  openSkillPane(name);
}

function renderHealth() {
  const el = document.getElementById("health-row");
  if (!state.overview) return;
  const n = state.overview.net_harmful_count || 0;
  const noise = state.overview.noise_verdict_count || 0;
  const orphan = state.overview.orphan_count || 0;
  const unknown = state.overview.unknown_host_count || 0;
  const parts = [];
  if (n > 0) {
    parts.push(
      `<span class="health-warn">⚠ ${n} net-harmful skill${n > 1 ? "s" : ""}</span>` +
      ` <a class="health-link" data-act="net-harmful">show</a>`,
    );
  }
  if (noise > 0) {
    parts.push(
      `<span class="health-warn">⚠ ${noise} low-quality verdict${noise > 1 ? "s" : ""}</span>` +
      ` <a class="health-link" data-act="noise">show</a>`,
    );
  }
  if (orphan > 0) {
    parts.push(
      `<span class="health-warn">⚠ ${orphan} orphan skill${orphan > 1 ? "s" : ""}</span>` +
      ` <a class="health-link" data-act="orphan">show</a>` +
      ` <span class="health-hint">(verdicts exist but SKILL.md isn't on disk — usually benchmark or stale catalog leftovers)</span>`,
    );
  }
  if (unknown > 0) {
    parts.push(
      `<span class="health-warn">⚠ ${unknown} skill${unknown > 1 ? "s" : ""} installed under a non-standard root</span>`,
    );
  }
  // No warnings → keep the row empty so the filter widget alongside
  // takes the visual center. The Big Numbers Health card on the
  // overview tab already calls out "✓ all clear" globally; we don't
  // need to repeat it here.
  el.innerHTML = parts.length === 0 ? "" : parts.join(" &middot; ");
  el.hidden = parts.length === 0;
  if (parts.length === 0) return;
  el.querySelectorAll(".health-link").forEach((a) => {
    a.addEventListener("click", () => {
      // Health-row links live inside the Review tab, so flipping the
      // tab is a no-op when the link is clicked from there. Still
      // safe to call — setActiveTab short-circuits when already on
      // the target tab.
      if (a.dataset.act === "net-harmful") {
        state.netHarmfulFilter = true;
        state.orphanFilter = false;
        state.listMode = "skills";
        setActiveTab("review");
        syncListModeButtons();
        renderMainList();
      } else if (a.dataset.act === "noise") {
        state.noiseFilter = true;
        state.listMode = "verdicts";
        setActiveTab("review");
        syncListModeButtons();
        renderMainList();
      } else if (a.dataset.act === "orphan") {
        setActiveTab("review");
        openOrphanPane();
      }
    });
  });
}

function isPlaceholderReason(r) {
  if (r == null) return true;
  const s = String(r).trim().toLowerCase();
  if (s.length === 0) return true;
  if (s.length < 8) return true;
  const placeholders = new Set([
    "ok", "okay", "yes", "no", "n/a", "na",
    "test", "tests", "testing", "todo", "tbd",
    "fix", "fixed", "broken", "evidence a", "evidence b", "evidence c",
    "good", "bad", "fine", "true", "false",
    "x", "y", "z", "?", "??", "???", "-", "--", "...",
  ]);
  return placeholders.has(s);
}

// ---------- Main list (two modes: skills | verdicts) ---------- //

function syncListModeButtons() {
  document.querySelectorAll("[data-list-mode]").forEach((b) => {
    b.classList.toggle("active", b.dataset.listMode === state.listMode);
  });
  document.getElementById("list-mode-title").textContent =
    state.listMode === "skills" ? "Skills with verdicts" : "Recent verdicts";
}

function renderMainList() {
  if (state.listMode === "skills") {
    renderSkillList();
  } else {
    renderVerdictList();
  }
}

function renderSkillList() {
  const list = document.getElementById("verdicts-list");
  list.className = "skills-list";
  list.innerHTML = "";
  let rows = state.skillsByName.filter((s) => s.used);
  if (state.orphanFilter) rows = rows.filter((r) => r.orphan);
  if (state.netHarmfulFilter) rows = rows.filter((r) => r.net < 0);
  if (state.hostFilter) {
    rows = rows.filter((r) => (r.hosts_seen || []).includes(state.hostFilter));
  }
  rows = _applyReviewFilter(rows, /* kind */ "skill");

  if (rows.length === 0) {
    const li = document.createElement("li");
    li.className = "verdict-row";
    li.style.color = "var(--fg-muted)";
    li.style.gridTemplateColumns = "1fr";
    li.textContent = state.orphanFilter
      ? "no orphan skills"
      : state.netHarmfulFilter
        ? "no net-harmful skills"
        : (state.hostFilter || state.reviewFilter.value || state.reviewFilter.field === "hosts")
          ? "no skills match the current filter"
          : "no skills with verdicts yet";
    list.appendChild(li);
    _renderPager("review-pager", { page: 0, pageCount: 1, total: 0, onChange: () => {} });
    return;
  }

  const pageSize = PAGE_SIZE_TAB2;
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  let page = state.pages.skillsReview || 0;
  if (page >= pageCount) page = pageCount - 1;
  if (page < 0) page = 0;
  state.pages.skillsReview = page;
  const start = page * pageSize;
  const slice = rows.slice(start, start + pageSize);
  for (const r of slice) {
    list.appendChild(renderSkillListRow(r));
  }
  _renderPager("review-pager", {
    page,
    pageCount,
    total: rows.length,
    onChange: (next) => {
      state.pages.skillsReview = next;
      renderAll();
    },
  });
}

function renderSkillListRow(skill) {
  const li = document.createElement("li");
  li.className = "skill-row";
  if (skill.orphan) li.classList.add("orphan");
  const totalVerdicts = skill.helpful_count + skill.harmful_count + skill.neutral_count;
  // Only render the count pills that actually have a non-zero value —
  // empty pills are noise.
  const pills = [];
  if (skill.helpful_count) pills.push(`<span class="count-pill HELPFUL" title="helpful">✓ ${skill.helpful_count}</span>`);
  if (skill.harmful_count) pills.push(`<span class="count-pill HARMFUL" title="harmful">✗ ${skill.harmful_count}</span>`);
  if (skill.neutral_count) pills.push(`<span class="count-pill NEUTRAL" title="neutral">· ${skill.neutral_count}</span>`);
  li.innerHTML = `
    <span class="skill-name" title="${escapeAttr(skill.name)}">${escapeHtml(skill.name)}</span>
    <span class="count-pills">${pills.join(" ")}</span>
    <span class="net ${skill.net > 0 ? "pos" : skill.net < 0 ? "neg" : ""}">net ${skill.net}</span>
    <span class="meta-mini">${totalVerdicts} verdict${totalVerdicts === 1 ? "" : "s"}${skill.orphan ? " · orphan" : ""}</span>
  `;
  li.addEventListener("click", () => openSkillPane(skill.name));
  return li;
}

function renderVerdictList() {
  const list = document.getElementById("verdicts-list");
  list.className = "verdicts-list";
  list.innerHTML = "";
  let rows = state.verdicts;
  if (state.noiseFilter) rows = rows.filter((v) => isPlaceholderReason(v.reason));
  if (state.hostFilter) {
    rows = rows.filter((v) => (v.host_raw || v.host) === state.hostFilter
      || v.host === state.hostFilter);
  }
  rows = _applyReviewFilter(rows, /* kind */ "verdict");

  if (rows.length === 0) {
    const li = document.createElement("li");
    li.className = "verdict-row";
    li.style.color = "var(--fg-muted)";
    li.style.gridTemplateColumns = "1fr";
    li.textContent = state.noiseFilter
      ? "no placeholder-reason verdicts"
      : (state.reviewFilter.value || state.reviewFilter.field === "hosts")
        ? "no verdicts match the current filter"
        : "no verdicts yet";
    list.appendChild(li);
    _renderPager("review-pager", { page: 0, pageCount: 1, total: 0, onChange: () => {} });
    return;
  }

  const pageSize = PAGE_SIZE_TAB2;
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  let page = state.pages.verdictsReview || 0;
  if (page >= pageCount) page = pageCount - 1;
  if (page < 0) page = 0;
  state.pages.verdictsReview = page;
  const start = page * pageSize;
  const slice = rows.slice(start, start + pageSize);
  for (const v of slice) {
    const li = document.createElement("li");
    li.className = "verdict-row";
    li.innerHTML = `
      <span class="when">${relTime(v.occurred_at)}</span>
      <span class="host-tag">${v.host}</span>
      <span class="skill">${escapeHtml(v.skill_name)}</span>
      <span class="label ${v.verdict}">${verdictGlyph(v.verdict)} ${v.verdict.toLowerCase()}</span>
      <span class="reason" title="${escapeAttr(v.reason || "")}">${escapeHtml(v.reason || "")}</span>
    `;
    li.addEventListener("click", () => openVerdictDetailFromList(v));
    list.appendChild(li);
  }
  _renderPager("review-pager", {
    page,
    pageCount,
    total: rows.length,
    onChange: (next) => {
      state.pages.verdictsReview = next;
      renderAll();
    },
  });
}

// Shared filter: applies state.reviewFilter against either a skill
// row (skillsByName item) or a verdict row. String match is
// case-insensitive substring; the "hosts" category is exact-match
// against the dropdown value. Empty filter passes everything.
function _applyReviewFilter(rows, kind) {
  const { field, value } = state.reviewFilter || { field: "title", value: "" };
  if (!value) return rows;
  const q = String(value).trim().toLowerCase();
  if (!q) return rows;
  return rows.filter((r) => {
    if (field === "title") {
      const name = kind === "skill" ? r.name : r.skill_name;
      return (name || "").toLowerCase().includes(q);
    }
    if (field === "description") {
      // skill rows carry no description (it lives in skill_detail);
      // fall back to name so the filter at least narrows something
      // sensible. Verdict rows match against reason text instead.
      const text = kind === "skill"
        ? (r.description || r.name || "")
        : (r.reason || "");
      return text.toLowerCase().includes(q);
    }
    if (field === "hosts") {
      // Exact-match against the host shortname dropdown.
      if (kind === "skill") {
        return (r.hosts_seen || []).includes(q)
          || (r.installed_hosts || []).includes(q);
      }
      const h = (r.host || r.host_raw || "").toLowerCase();
      return h === q;
    }
    return true;
  });
}

// ---------- Multi-pane rail ---------- //

// Single-pane model: clicking another skill REPLACES the open skill
// pane (not stacks). Inside the pane, helpful/harmful/neutral expand
// inline; clicking a verdict opens its editor inline below the row.
function openSkillPane(name) {
  // If a pane for this skill is already open, no-op (don't stack).
  const existing = state.panes.find(
    (p) => p.kind === "skill" && (p.name === name || (p.payload && p.payload.name === name)),
  );
  if (existing) return;
  // Replace: at most one pane at a time.
  state.panes = [{
    id: nextPaneId(),
    kind: "skill",
    payload: null,
    name,
    expanded: null,           // null | "HELPFUL" | "HARMFUL" | "NEUTRAL"
    expandedVerdictId: null,  // id of the verdict whose edit panel is open
    composerOpen: false,      // is the "Add verdict" composer expanded?
    composerVerdict: "HELPFUL",
    composerReason: "",
  }];
  renderPanes();
  loadSkillPane(state.panes[0]);
}

function openVerdictDetailFromList(verdict) {
  // From the "Recent verdicts" main-list mode — opening the parent
  // skill pane and auto-expanding the right bucket gets the user to
  // the same data without spinning up a second pane.
  openSkillPane(verdict.skill_name);
  // Stash a hint so the skill pane, once loaded, expands the right
  // bucket and highlights this verdict.
  const pane = state.panes[0];
  if (pane) {
    // Don't filter — just highlight and open the row.
    pane.highlightVerdictId = verdict.id;
    pane.expandedVerdictId = verdict.id;
  }
}

function nextPaneId() { return state.paneSeq++; }

function closePane(id) {
  state.panes = state.panes.filter((p) => p.id !== id);
  renderPanes();
}

function clearAllPanes() {
  state.panes = [];
  renderPanes();
}

function scrollPaneIntoView(id) {
  const el = document.querySelector(`[data-pane-id='${id}']`);
  if (!el) return;
  // With flex-direction:row-reverse the newest pane is at scrollLeft=0
  // in DOM terms but at the visual right edge. Just scroll the rail
  // to its rightmost position so the newest pane is fully visible.
  const rail = document.getElementById("pane-rail");
  if (rail) {
    // Use rAF so the layout settles before we scroll.
    requestAnimationFrame(() => {
      rail.scrollTo({ left: 0, behavior: "smooth" });
    });
  }
}

async function loadSkillPane(pane) {
  try {
    const payload = await fetchJSON(`/api/skill/${encodeURIComponent(pane.name)}`);
    pane.payload = payload;
    renderPanes();
  } catch (err) {
    console.error(err);
    toast("Failed to load skill");
    closePane(pane.id);
  }
}

// ---------- Orphan pane (list + checkbox + bulk delete) ---------- //
//
// An orphan is a skill_name with verdict history in store.db but no
// SKILL.md on disk under any registered root. The pane lets the user
// see which directory it was last in (so they can recognise it as a
// benchmark / fixture leftover) and wipe its history in one shot.

function openOrphanPane() {
  const existing = state.panes.find((p) => p.kind === "orphan");
  if (existing) return;
  state.panes = [{
    id: nextPaneId(),
    kind: "orphan",
    payload: null,        // [{name, total, hosts, last_seen_dir, ...}]
    selected: new Set(),  // names checked for bulk delete
    submitting: false,
  }];
  renderPanes();
  loadOrphanPane(state.panes[0]);
}

async function loadOrphanPane(pane) {
  try {
    pane.payload = await fetchJSON("/api/orphans");
    renderPanes();
  } catch (err) {
    console.error(err);
    toast("Failed to load orphans");
    closePane(pane.id);
  }
}

function renderOrphanPaneBody(pane) {
  const wrap = document.createElement("div");
  wrap.className = "orphan-pane-content";
  if (!pane.payload) {
    wrap.innerHTML = `<div class="meta-line">loading…</div>`;
    return wrap;
  }
  const rows = pane.payload;
  if (rows.length === 0) {
    wrap.innerHTML = `<div class="meta-line">No orphan skills. 🎉</div>`;
    return wrap;
  }

  const allChecked = rows.length > 0 && rows.every((r) => pane.selected.has(r.name));
  const someChecked = pane.selected.size > 0;
  const intro = document.createElement("div");
  intro.className = "meta-line";
  intro.innerHTML =
    `<strong>${rows.length} orphan skill${rows.length === 1 ? "" : "s"}</strong> — ` +
    `verdicts exist in <code>store.db</code> but no <code>SKILL.md</code> ` +
    `is on disk. Usually benchmark / fixture leftovers; safe to clean up.`;
  wrap.appendChild(intro);

  // Toolbar: select-all + delete-selected
  const toolbar = document.createElement("div");
  toolbar.className = "actions orphan-toolbar";
  toolbar.innerHTML = `
    <label class="orphan-select-all">
      <input type="checkbox" data-act="toggle-all" ${allChecked ? "checked" : ""}>
      <span>Select all</span>
    </label>
    <button class="action danger" data-act="delete-selected"
            ${someChecked && !pane.submitting ? "" : "disabled"}>
      ${pane.submitting
        ? "Deleting…"
        : `Delete selected (${pane.selected.size})`}
    </button>
  `;
  wrap.appendChild(toolbar);

  // Rows
  const list = document.createElement("ul");
  list.className = "orphan-list";
  for (const row of rows) {
    const li = document.createElement("li");
    li.className = "orphan-row";
    const checked = pane.selected.has(row.name);
    const lastDir = row.last_seen_dir
      ? `<div class="orphan-meta" title="${escapeAttr(row.last_seen_dir)}">📁 ${escapeHtml(row.last_seen_dir)}</div>`
      : `<div class="orphan-meta orphan-meta-empty">📁 <em>no directory recorded</em> (migration-era row)</div>`;
    const hostsStr = row.hosts.length ? row.hosts.join(", ") : "—";
    const countsStr =
      `${row.helpful}H · ${row.harmful}X · ${row.neutral}N`;
    li.innerHTML = `
      <label class="orphan-check">
        <input type="checkbox" data-name="${escapeAttr(row.name)}" ${checked ? "checked" : ""}>
      </label>
      <div class="orphan-body">
        <div class="orphan-name"><code>${escapeHtml(row.name)}</code></div>
        ${lastDir}
        <div class="orphan-meta orphan-meta-stats">
          <span title="HELPFUL / HARMFUL / NEUTRAL">${countsStr}</span>
          <span>·</span>
          <span title="hosts that recorded a verdict">hosts: ${escapeHtml(hostsStr)}</span>
          ${row.last_updated
            ? `<span>·</span><span title="most recent verdict">last: ${escapeHtml(String(row.last_updated).slice(0, 10))}</span>`
            : ""}
        </div>
      </div>
    `;
    list.appendChild(li);
  }
  wrap.appendChild(list);

  // Wire toolbar
  toolbar.querySelector('[data-act="toggle-all"]').addEventListener("change", (ev) => {
    if (ev.target.checked) {
      for (const r of rows) pane.selected.add(r.name);
    } else {
      pane.selected.clear();
    }
    renderPanes();
  });
  toolbar.querySelector('[data-act="delete-selected"]').addEventListener("click", () => {
    deleteSelectedOrphans(pane);
  });

  // Wire row checkboxes
  list.querySelectorAll('input[type="checkbox"][data-name]').forEach((cb) => {
    cb.addEventListener("change", () => {
      const name = cb.dataset.name;
      if (cb.checked) pane.selected.add(name);
      else pane.selected.delete(name);
      // Re-render so the toolbar button label/disabled state updates.
      renderPanes();
    });
  });

  return wrap;
}

async function deleteSelectedOrphans(pane) {
  const names = Array.from(pane.selected);
  if (names.length === 0) return;
  const plural = names.length === 1 ? "" : "s";
  const ok = await showConfirm({
    title: `Delete ${names.length} orphan skill${plural}?`,
    body:
      `This removes every verdict row plus the skills-table entry ` +
      `for the selected name${plural}. It cannot be undone.`,
    sub: names.slice(0, 5).join(", ") + (names.length > 5
      ? `, +${names.length - 5} more` : ""),
    confirmLabel: "Delete",
    confirmKind: "danger",
  });
  if (!ok) return;
  pane.submitting = true;
  renderPanes();
  try {
    const resp = await fetch("/api/orphans/delete-bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${resp.status}`);
    }
    const result = await resp.json();
    const deletedCount = result.deleted.length;
    const skippedCount = result.skipped.length;
    let msg = `Removed ${deletedCount} orphan${deletedCount === 1 ? "" : "s"}`;
    if (skippedCount > 0) {
      msg += ` (${skippedCount} skipped — see console)`;
      console.warn("Skipped orphans:", result.skipped);
    }
    toast(msg);
    pane.selected.clear();
    // Refresh both the orphan list and the overview health row.
    await loadOrphanPane(pane);
    loadAll();
  } catch (err) {
    console.error(err);
    toast(`Delete failed: ${err.message || err}`);
  } finally {
    pane.submitting = false;
    renderPanes();
  }
}

function renderPanes() {
  const rail = document.getElementById("pane-rail");

  // Preserve scroll positions across full re-renders. Without this,
  // every renderPanes() call (e.g. toggling a verdict row open/closed)
  // would reset the pane to scrollTop=0 — the symptom the user sees as
  // "the sidebar jumps to the top". We snapshot per-paneId so the IDs
  // survive the destroy/rebuild.
  const scrollSnapshot = {};
  for (const aside of rail.querySelectorAll(".pane")) {
    const id = aside.dataset.paneId;
    if (!id) continue;
    scrollSnapshot[id] = {
      paneTop: aside.scrollTop,
      historyTop: (aside.querySelector(".history-section") || {}).scrollTop || 0,
    };
  }

  rail.innerHTML = "";
  if (state.panes.length === 0) {
    rail.classList.remove("open");
    document.body.classList.remove("rail-open");
    return;
  }
  rail.classList.add("open");
  document.body.classList.add("rail-open");

  let newestNeedsScroll = true;
  for (const pane of state.panes) {
    const node = renderOnePane(pane);
    rail.appendChild(node);
    const snap = scrollSnapshot[String(pane.id)];
    if (snap) {
      // Apply on the next frame so the new node has its layout pass.
      requestAnimationFrame(() => {
        node.scrollTop = snap.paneTop;
        const hs = node.querySelector(".history-section");
        if (hs) hs.scrollTop = snap.historyTop;
      });
      newestNeedsScroll = false; // returning to saved view, not a new pane
    }
  }
  // Only do the "scroll the new pane into view" gesture when a pane
  // was actually appended fresh (no snapshot for any pane).
  const last = state.panes[state.panes.length - 1];
  if (newestNeedsScroll && !scrollSnapshot[String(last.id)]) {
    scrollPaneIntoView(last.id);
  }
}

// --- Pane resize ----------------------------------------------------------
// Panes default to 420px (the value baked into the CSS). The user can
// drag the left edge to grow/shrink them; the chosen width is mirrored
// onto the document root as `--pane-width` and persisted to
// localStorage. Clamp range matches what the CSS treats as sensible:
// below 320px the header crowds the close button, above 1200px the
// pane covers more than half of a typical laptop viewport.
const PANE_WIDTH_MIN = 320;
const PANE_WIDTH_MAX = 1200;
const PANE_WIDTH_DEFAULT = 420;
const PANE_WIDTH_STORAGE_KEY = "megaTronPaneWidth";

function _readPaneWidth() {
  try {
    const raw = localStorage.getItem(PANE_WIDTH_STORAGE_KEY);
    if (!raw) return PANE_WIDTH_DEFAULT;
    const n = parseInt(raw, 10);
    if (!Number.isFinite(n)) return PANE_WIDTH_DEFAULT;
    return Math.min(PANE_WIDTH_MAX, Math.max(PANE_WIDTH_MIN, n));
  } catch (_e) {
    return PANE_WIDTH_DEFAULT;
  }
}

function _applyPaneWidth(px) {
  document.documentElement.style.setProperty("--pane-width", `${px}px`);
}

// Apply persisted width on first script load so the very first pane
// opened in the session honors the user's choice without flicker.
_applyPaneWidth(_readPaneWidth());

function _onPaneResizeStart(ev) {
  // Only react to primary button; ignore middle/right/macOS ctrl-click.
  if (ev.button !== 0) return;
  ev.preventDefault();
  const startX = ev.clientX;
  const startWidth = _readPaneWidth();
  document.body.classList.add("pane-resizing");

  const onMove = (mv) => {
    // Pane is anchored to the right edge of the viewport, so dragging
    // LEFT (smaller clientX) grows the pane. delta = startX - clientX.
    const next = Math.min(
      PANE_WIDTH_MAX,
      Math.max(PANE_WIDTH_MIN, startWidth + (startX - mv.clientX)),
    );
    _applyPaneWidth(next);
  };
  const onUp = () => {
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
    document.body.classList.remove("pane-resizing");
    // Read the px value back from the variable (already clamped during
    // the move) so we persist exactly what the user saw on release.
    const final = parseInt(
      getComputedStyle(document.documentElement).getPropertyValue("--pane-width"),
      10,
    ) || PANE_WIDTH_DEFAULT;
    try {
      localStorage.setItem(PANE_WIDTH_STORAGE_KEY, String(final));
    } catch (_e) {
      // localStorage may be disabled in incognito; resizing still works
      // for the current session, only the next reload won't remember.
    }
  };
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
}

function _onPaneResizeReset() {
  _applyPaneWidth(PANE_WIDTH_DEFAULT);
  try {
    localStorage.removeItem(PANE_WIDTH_STORAGE_KEY);
  } catch (_e) { /* see _onPaneResizeStart */ }
}

function renderOnePane(pane) {
  const aside = document.createElement("aside");
  aside.className = `pane pane-${pane.kind}`;
  aside.dataset.paneId = String(pane.id);

  // Left-edge resize handle. Every pane carries its own grip; the
  // drag updates a single `--pane-width` CSS variable on document
  // root, so all open panes resize together (no per-pane jitter when
  // a stack of 2-3 panes is open) and the choice persists across
  // sessions via localStorage.
  const handle = document.createElement("div");
  handle.className = "pane-resize-handle";
  handle.setAttribute("role", "separator");
  handle.setAttribute("aria-orientation", "vertical");
  handle.setAttribute("aria-label", "Resize pane");
  handle.title = "Drag to resize · double-click to reset";
  handle.addEventListener("mousedown", _onPaneResizeStart);
  handle.addEventListener("dblclick", _onPaneResizeReset);
  aside.appendChild(handle);

  // Header is kind-specific: skill panes title with the skill name
  // and a "N uses" badge from payload.total_verdicts. Orphan panes
  // are list views, not single-record — title them by what the user
  // is looking at, and show the count once payload arrives.
  let titleText;
  let usageBadge = "";
  if (pane.kind === "orphan") {
    const count = Array.isArray(pane.payload) ? pane.payload.length : null;
    titleText = "Orphan skills";
    if (count !== null) {
      usageBadge = `<span class="usage-badge" title="orphan skills found">${count}</span>`;
    }
  } else {
    titleText = pane.payload ? pane.payload.name : pane.name;
    const total = pane.payload && pane.payload.total_verdicts;
    usageBadge = total
      ? `<span class="usage-badge" title="${total} verdict${total === 1 ? "" : "s"} recorded">${total} use${total === 1 ? "" : "s"}</span>`
      : "";
  }
  const head = document.createElement("header");
  head.className = "pane-head";
  head.innerHTML = `
    <h2>${escapeHtml(titleText || "…")}</h2>
    ${usageBadge}
    <button class="close" aria-label="Close pane">×</button>
  `;
  head.querySelector(".close").addEventListener("click", () => closePane(pane.id));
  aside.appendChild(head);

  const body = document.createElement("div");
  body.className = "pane-body";
  aside.appendChild(body);
  if (pane.kind === "orphan") {
    body.appendChild(renderOrphanPaneBody(pane));
  } else {
    body.appendChild(renderSkillPaneBody(pane));
  }
  return aside;
}

function renderSkillPaneBody(pane) {
  const wrap = document.createElement("div");
  wrap.className = "skill-pane-content";
  if (!pane.payload) {
    wrap.innerHTML = `<div class="meta-line">loading…</div>`;
    return wrap;
  }
  const p = pane.payload;
  const hc = p.helpful_count || 0;
  const xc = p.harmful_count || 0;
  const nc = p.neutral_count || 0;

  const pathBlock = p.skill_dir
    ? `<a href="#" class="path-jump" data-path="${escapeAttr(p.skill_dir)}">📁 ${escapeHtml(p.skill_dir)}</a>`
    : `<div class="meta-line orphan-warn">⚠ orphan: no SKILL.md on disk (benchmark artefact or stale catalog)</div>`;

  const manageBlock = p.skill_dir
    ? `<div class="card-title">Manage</div>
       <div class="actions">
         ${p.status === "archived"
           ? `<button class="action danger" data-act="delete-skill">Delete forever</button>`
           : `<button class="action" data-act="archive-skill">Archive</button>`}
       </div>`
    : `<div class="card-title">Manage</div>
       <div class="actions">
         <button class="action danger" data-act="bulk-delete-orphan">Delete all verdicts for this orphan</button>
       </div>`;

  const expanded = pane.expanded;
  // Count pills live in the History section header (one click = expand
  // that bucket). The top summary keeps only the durable summaries
  // (net + status) so the user's eye doesn't have to bounce.
  const countPills = [];
  if (hc > 0) countPills.push(`<button class="pill-stat pos count-btn ${expanded === "HELPFUL" ? "open" : ""}" data-bucket="HELPFUL">✓ ${hc} helpful</button>`);
  if (xc > 0) countPills.push(`<button class="pill-stat neg count-btn ${expanded === "HARMFUL" ? "open" : ""}" data-bucket="HARMFUL">✗ ${xc} harmful</button>`);
  if (nc > 0) countPills.push(`<button class="pill-stat count-btn ${expanded === "NEUTRAL" ? "open" : ""}" data-bucket="NEUTRAL">· ${nc} neutral</button>`);
  const hasAnyCounts = countPills.length > 0;

  const descBlock = p.description
    ? `<div class="skill-description">${escapeHtml(p.description)}</div>`
    : "";

  wrap.innerHTML = `
    ${descBlock}
    <div class="skill-summary">
      <span class="pill-stat ${p.net > 0 ? "pos" : p.net < 0 ? "neg" : ""}">net ${p.net}</span>
      <span class="pill-stat">${p.status}</span>
    </div>
    <div class="path-row">${pathBlock}</div>
    <div class="meta-line subtle">last activity: ${p.last_updated || "never"}</div>

    <div class="card-title">Per-host comparison</div>
    <div class="per-host-chart"></div>

    <div class="card-title">Activity (30 days)</div>
    <svg class="mini-sparkline" viewBox="0 0 300 28" preserveAspectRatio="none"></svg>

    <div class="history-block">
      <div class="history-header">
        <span class="card-title history-title">History</span>
        ${hasAnyCounts
          ? `<span class="history-pills">${countPills.join("")}</span>`
          : `<span class="meta-line subtle">no verdicts yet</span>`}
        <button class="add-verdict-btn ${pane.composerOpen ? "open" : ""}" type="button"
                title="Add your own verdict for this skill">
          ${pane.composerOpen ? "× cancel" : "+ Add verdict"}
        </button>
      </div>
      <div class="composer-section"></div>
      <div class="history-section"></div>
    </div>

    ${manageBlock}
  `;

  // Per-host bar chart.
  renderPerHostChart(wrap.querySelector(".per-host-chart"), p);
  // Mini sparkline.
  drawMiniSpark(wrap.querySelector(".mini-sparkline"), p.sparkline);

  // Inline history block.
  // Default = show every verdict. Clicking a pill (helpful/harmful/
  // neutral) filters the list to that label; clicking the same pill
  // again clears the filter.
  const historySection = wrap.querySelector(".history-section");
  if (hasAnyCounts) {
    historySection.appendChild(renderInlineHistory(pane, expanded));
  }

  // Composer (Add verdict).
  const composerSection = wrap.querySelector(".composer-section");
  if (pane.composerOpen) {
    composerSection.appendChild(renderVerdictComposer(pane));
  }
  wrap.querySelector(".add-verdict-btn").addEventListener("click", () => {
    pane.composerOpen = !pane.composerOpen;
    if (!pane.composerOpen) {
      pane.composerReason = "";
    }
    renderPanes();
  });

  // Wire pill toggles.
  wrap.querySelectorAll(".count-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const bucket = btn.dataset.bucket;
      pane.expanded = pane.expanded === bucket ? null : bucket;
      pane.expandedVerdictId = null;
      renderPanes();
    });
  });

  // Manage actions.
  wrap.querySelectorAll(".actions .action").forEach((btn) => {
    btn.addEventListener("click", () => skillAction(p, btn.dataset.act, btn));
  });

  // Path-jump.
  const pathJump = wrap.querySelector(".path-jump");
  if (pathJump) {
    pathJump.addEventListener("click", async (e) => {
      e.preventDefault();
      await openFolder(pathJump.dataset.path);
    });
  }
  return wrap;
}

function renderInlineHistory(pane, bucket) {
  // bucket = null  → show ALL verdicts (default)
  // bucket = "HELPFUL"|"HARMFUL"|"NEUTRAL" → filter to that label
  const p = pane.payload;
  const wrap = document.createElement("div");
  let items;
  if (bucket) {
    const key = bucket === "HELPFUL" ? "helpful_history"
      : bucket === "HARMFUL" ? "harmful_history" : "neutral_history";
    items = p[key] || [];
  } else {
    // Merge all three streams; sort by occurred_at desc.
    items = [
      ...(p.helpful_history || []),
      ...(p.harmful_history || []),
      ...(p.neutral_history || []),
    ].sort((a, b) => (a.occurred_at < b.occurred_at ? 1 : -1));
  }
  if (items.length === 0) {
    wrap.innerHTML = `<div class="meta-line subtle">(none)</div>`;
    return wrap;
  }
  // No hoisting — the history list now has its own internal scroll
  // viewport (.history-section is overflow:auto), so the expanded row
  // can stay in its chronological position and we just scroll the
  // container to keep it visible.
  wrap.className = "history-list";
  for (const v of items) {
    wrap.appendChild(renderInlineHistoryRow(v, pane));
  }
  return wrap;
}

function renderVerdictComposer(pane) {
  const skill = pane.payload.name;
  const wrap = document.createElement("div");
  wrap.className = "composer-card";
  // NEUTRAL intentionally absent: a user filing a manual verdict
  // always has an opinion (the implicit "no opinion" is just not filing).
  const choices = ["HELPFUL", "HARMFUL"];
  if (pane.composerVerdict === "NEUTRAL") pane.composerVerdict = "HELPFUL";
  wrap.innerHTML = `
    <label class="panel-label">You're adding a verdict as <span class="composer-host-tag">user</span></label>
    <div class="composer-choice">
      ${choices.map((v) => `
        <button class="composer-pick ${v} ${pane.composerVerdict === v ? "active" : ""}" data-pick="${v}">
          ${verdictGlyph(v)} ${v}
        </button>
      `).join("")}
    </div>
    <label class="panel-label">Reason <span class="composer-optional">(optional)</span></label>
    <textarea class="reason-textarea" rows="3" placeholder="Why does this skill deserve this label? (≥ 8 chars to be saved with a reason; leave blank for label-only)"></textarea>
    <div class="composer-footer">
      <button class="action ghost" data-act="cancel" type="button">Cancel</button>
      <button class="action primary" data-act="submit" type="button">Save</button>
    </div>
  `;
  const ta = wrap.querySelector("textarea");
  ta.value = pane.composerReason || "";
  // Save reason draft on input so toggling the verdict button doesn't
  // lose what the user typed.
  ta.addEventListener("input", () => { pane.composerReason = ta.value; });
  setTimeout(() => ta.focus(), 0);

  wrap.querySelectorAll(".composer-pick").forEach((btn) => {
    btn.addEventListener("click", () => {
      pane.composerVerdict = btn.dataset.pick;
      // Persist reason draft before re-render.
      pane.composerReason = ta.value;
      renderPanes();
    });
  });

  const submit = async () => {
    const reason = ta.value.trim();
    try {
      const resp = await fetch("/api/verdict", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          skill_name: skill,
          verdict: pane.composerVerdict,
          reason: reason || null,
        }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.error || `HTTP ${resp.status}`);
      }
      toast(`Added ${pane.composerVerdict.toLowerCase()} verdict`);
      pane.composerOpen = false;
      pane.composerReason = "";
      await loadAll();
      await refreshOpenPanes();
    } catch (err) {
      console.error(err);
      toast(`Add failed: ${err.message}`);
    }
  };
  wrap.querySelector("[data-act='submit']").addEventListener("click", submit);
  const cancel = () => {
    pane.composerOpen = false;
    pane.composerReason = "";
    renderPanes();
  };
  wrap.querySelector("[data-act='cancel']").addEventListener("click", cancel);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submit(); }
    else if (e.key === "Escape") { e.preventDefault(); cancel(); }
  });
  return wrap;
}


function renderInlineHistoryRow(verdict, pane) {
  const expanded = pane.expandedVerdictId === verdict.id;
  const li = document.createElement("div");
  li.className = "history-row" + (expanded ? " expanded" : "");
  if (pane.highlightVerdictId === verdict.id) li.classList.add("highlight");

  if (!expanded) {
    // Collapsed: time / host / reason-preview / chevron. Clicking
    // anywhere on this row pops it open.
    const header = document.createElement("button");
    header.type = "button";
    header.className = "history-row-head";
    header.innerHTML = `
      <span class="when">${relTime(verdict.occurred_at)}</span>
      <span class="host-tag ${verdict.host}">${verdict.host}</span>
      <span class="row-reason-preview">${escapeHtml(verdict.reason || "(no reason)")}</span>
      <span class="chevron">▸</span>
    `;
    header.addEventListener("click", () => {
      pane.expandedVerdictId = verdict.id;
      renderPanes();
    });
    li.appendChild(header);
    return li;
  }

  // Expanded: header keeps time + host, but the chevron is replaced
  // by inline action buttons. The reason becomes an inline-editable
  // textarea right below — no nested panel, no sticky footer, no
  // wrapping action stripes. Just two stacked rows.
  const current = verdict.verdict;
  const others = ["HELPFUL", "HARMFUL", "NEUTRAL"].filter((v) => v !== current);

  const head = document.createElement("div");
  head.className = "history-row-head expanded-head";
  // Collapse affordance: clicking the (always-visible) header strip
  // outside the action buttons closes the row. The explicit ▾ button
  // is gone because clicking the row itself OR pressing Esc already
  // collapses it — one less control to misread.
  head.innerHTML = `
    <span class="when">${relTime(verdict.occurred_at)}</span>
    <span class="host-tag ${verdict.host}">${verdict.host}</span>
    <span class="row-actions-inline">
      ${others.map((v) => `
        <button class="row-act ${v}" data-relabel="${v}"
                title="Re-label as ${v.toLowerCase()}">
          ${verdictGlyph(v)} ${v.toLowerCase()}
        </button>
      `).join("")}
      <button class="row-act danger" data-act="delete-here"
              title="Delete this verdict">✗ delete</button>
    </span>
  `;
  li.appendChild(head);

  const reasonRow = document.createElement("div");
  reasonRow.className = "reason-row";
  reasonRow.innerHTML = `
    <textarea class="reason-textarea" rows="3" aria-label="reason"
              placeholder="(no reason — type one and press ⌘/Ctrl+Enter)">${escapeHtml(verdict.reason || "")}</textarea>
  `;
  li.appendChild(reasonRow);

  const ta = reasonRow.querySelector("textarea");
  setTimeout(() => { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }, 0);

  const collapse = () => { pane.expandedVerdictId = null; renderPanes(); };
  const saveReason = async () => {
    const next = ta.value.trim();
    if (next === (verdict.reason || "").trim()) { collapse(); return; }
    await patchVerdict(verdict.id, { reason: next }, "Reason updated");
    pane.expandedVerdictId = null;
  };

  ta.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); collapse(); }
    else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveReason(); }
  });
  // Blurring the textarea (clicking outside / tabbing away) saves
  // implicitly — keeps the row's edit-then-relabel flow zero-friction.
  ta.addEventListener("blur", () => {
    const next = ta.value.trim();
    if (next !== (verdict.reason || "").trim()) {
      patchVerdict(verdict.id, { reason: next }, "Reason updated").catch(() => {});
    }
  });

  // Clicking the header (anywhere except an action button) collapses
  // the row. We listen on the head and bail if the click target is a
  // button.
  head.addEventListener("click", (e) => {
    if (e.target.closest("button")) return;
    collapse();
  });
  head.querySelector("[data-act='delete-here']").addEventListener("click", async () => {
    const ok = await showConfirm({
      title: "Delete verdict",
      body: "Delete this verdict permanently?",
      sub: "Counts will recompute and this row will disappear.",
      confirmLabel: "Delete",
      confirmKind: "danger",
    });
    if (!ok) return;
    // Optimistic remove — drop the row from the DOM immediately so
    // the user sees the action take effect without waiting on the
    // server round-trip or the next renderPanes() pass. If the
    // request fails, deleteVerdict() will toast and the next refresh
    // will bring the row back.
    li.remove();
    await deleteVerdict(verdict.id);
  });
  head.querySelectorAll("[data-relabel]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const next = btn.dataset.relabel;
      const body = { verdict: next };
      const reasonNow = ta.value.trim();
      if (reasonNow !== (verdict.reason || "").trim()) {
        body.reason = reasonNow;
      }
      await patchVerdict(verdict.id, body, `→ ${next.toLowerCase()}`);
      pane.expandedVerdictId = null;
    });
  });

  // Scroll the expanded row into view INSIDE the history-section.
  // We use a two-rAF deferral so any scroll-position restoration
  // renderPanes() scheduled for the next frame has already applied.
  // Then we explicitly compute the scroll inside the history-section
  // container — native scrollIntoView would scroll the pane-body too.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const container = li.closest(".history-section");
    if (!container) return;
    // Use offsetTop-based math (relative to the container) so the row
    // is positioned right at the top of the history viewport. That
    // guarantees the textarea + action footer never get clipped, no
    // matter where the row sits in the list.
    container.scrollTo({
      top: Math.max(0, li.offsetTop - container.offsetTop - 4),
      behavior: "smooth",
    });
  }));

  return li;
}

function renderPerHostChart(container, payload) {
  // Matrix of host tiles. Each tile is a flat card with: the host
  // label, a big net score (color-coded), and a dotted distribution
  // row (one dot per verdict — green = helpful, red = harmful, grey
  // = neutral). When verdict count > 8 we collapse to "✓N ✗M" text.
  //
  // Why this beats stacked horizontal bars:
  //   - net score is the question the user is actually asking
  //     ("does this host like this skill?"). Single big number → fast read.
  //   - dot row maps 1:1 with verdicts, so a "3 helpful 1 harmful"
  //     mix is visually distinct from "3 helpful 3 harmful" — they had
  //     the same relative width in the old bar.
  //   - tile layout puts hosts side-by-side, so disagreement is
  //     a horizontal eye-movement, not a vertical scan.
  const byName = state.skillsByName.find((s) => s.name === payload.name);
  const perHost = (byName && byName.per_host) || (() => {
    const out = {};
    for (const [k, v] of Object.entries(payload.per_host || {})) {
      out[k] = { ...v, net: (v.helpful || 0) - (v.harmful || 0) };
    }
    return out;
  })();
  const present = HOSTS.filter((h) => perHost[h]);
  if (present.length === 0) {
    container.innerHTML = `<div class="meta-line subtle">No host-level verdicts yet.</div>`;
    return;
  }

  container.innerHTML = "";
  container.classList.add("phc-grid");

  // Detect disagreement so the chart can flag it: any pair of hosts
  // where one is net-positive and another net-negative.
  const nets = present.map((h) => perHost[h].net);
  const hasDisagreement = Math.max(...nets) > 0 && Math.min(...nets) < 0;
  if (hasDisagreement) {
    const flag = document.createElement("div");
    flag.className = "phc-disagreement";
    flag.innerHTML = `⚠ hosts disagree on this skill`;
    container.appendChild(flag);
  }

  for (const h of present) {
    const ph = perHost[h];
    const total = (ph.helpful || 0) + (ph.harmful || 0) + (ph.neutral || 0);
    let distribution = "";
    if (total <= 8) {
      // Dotted distribution.
      const dots = [];
      for (let i = 0; i < (ph.helpful || 0); i++) dots.push('<span class="phc-dot helpful" title="helpful"></span>');
      for (let i = 0; i < (ph.harmful || 0); i++) dots.push('<span class="phc-dot harmful" title="harmful"></span>');
      for (let i = 0; i < (ph.neutral || 0); i++) dots.push('<span class="phc-dot neutral" title="neutral"></span>');
      distribution = `<div class="phc-dots">${dots.join("")}</div>`;
    } else {
      // Too many to dot — show counts as text.
      const parts = [];
      if (ph.helpful) parts.push(`<span class="phc-c helpful">✓${ph.helpful}</span>`);
      if (ph.harmful) parts.push(`<span class="phc-c harmful">✗${ph.harmful}</span>`);
      if (ph.neutral) parts.push(`<span class="phc-c neutral">·${ph.neutral}</span>`);
      distribution = `<div class="phc-text">${parts.join(" ")}</div>`;
    }
    const netClass = ph.net > 0 ? "pos" : ph.net < 0 ? "neg" : "zero";
    const tile = document.createElement("div");
    tile.className = `phc-tile phc-${h} ${netClass}`;
    tile.innerHTML = `
      <div class="phc-tile-head">
        <span class="phc-tile-host">${h}</span>
        <span class="phc-tile-total">${total} verdict${total === 1 ? "" : "s"}</span>
      </div>
      <div class="phc-tile-net">${ph.net > 0 ? "+" : ""}${ph.net}</div>
      ${distribution}
    `;
    container.appendChild(tile);
  }
}

async function patchVerdict(id, body, msg) {
  try {
    const resp = await fetch(`/api/verdict/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`PATCH ${resp.status}`);
    toast(msg);
    await loadAll();
    await refreshOpenPanes();
  } catch (err) { console.error(err); toast("Update failed"); }
}

async function deleteVerdict(id) {
  try {
    const resp = await fetch(`/api/verdict/${id}`, { method: "DELETE" });
    if (!resp.ok) throw new Error(`DELETE ${resp.status}`);
    toast("Verdict deleted");
    // Close any verdict pane targeting this id.
    state.panes = state.panes.filter(
      (p) => !(p.kind === "verdict" && p.payload && p.payload.id === id),
    );
    await loadAll();
    await refreshOpenPanes();
  } catch (err) { console.error(err); toast("Delete failed"); }
}

async function refreshOpenPanes() {
  for (const pane of state.panes) {
    if (pane.kind === "skill" && pane.payload) {
      try {
        pane.payload = await fetchJSON(
          `/api/skill/${encodeURIComponent(pane.payload.name)}`,
        );
      } catch (_e) { /* leave stale */ }
    }
  }
  renderPanes();
}

async function verdictAction(verdict, act, btn) {
  const all = btn.parentElement.querySelectorAll(".action");
  all.forEach((b) => b.setAttribute("disabled", "disabled"));
  try {
    if (act === "delete") {
      const ok = await showConfirm({
        title: "Delete verdict",
        body: "Delete this verdict permanently?",
        sub: "Counts will recompute and this row will disappear.",
        confirmLabel: "Delete",
        confirmKind: "danger",
      });
      if (!ok) {
        all.forEach((b) => b.removeAttribute("disabled")); return;
      }
      await deleteVerdict(verdict.id);
    } else if (act === "flip" || act === "neutral") {
      const next = act === "neutral"
        ? "NEUTRAL"
        : (verdict.verdict === "HELPFUL" ? "HARMFUL"
          : verdict.verdict === "HARMFUL" ? "HELPFUL" : "NEUTRAL");
      await patchVerdict(verdict.id, { verdict: next }, `→ ${next.toLowerCase()}`);
    }
  } catch (err) {
    console.error(err); toast("Action failed");
    all.forEach((b) => b.removeAttribute("disabled"));
  }
}

async function skillAction(payload, act, btn) {
  const all = btn.parentElement.querySelectorAll(".action");
  all.forEach((b) => b.setAttribute("disabled", "disabled"));
  try {
    const name = payload.name;
    if (act === "archive-skill") {
      const ok = await showConfirm({
        title: "Archive skill",
        body: `Archive "${name}"?`,
        sub: "The router will stop surfacing it until you flip the YAML back.",
        confirmLabel: "Archive",
        confirmKind: "primary",
      });
      if (!ok) {
        all.forEach((b) => b.removeAttribute("disabled")); return;
      }
      const resp = await fetch(`/api/skill/${encodeURIComponent(name)}/archive`, { method: "POST" });
      if (!resp.ok) throw new Error(`archive failed: ${resp.status}`);
      toast(`Archived ${name}`);
      await loadAll();
      await refreshOpenPanes();
    } else if (act === "delete-skill") {
      const ok = await showConfirm({
        title: "Permanently delete skill",
        body: `Permanently delete "${name}"?`,
        sub: "Removes the directory from disk. Cannot be undone.",
        confirmLabel: "Continue",
        confirmKind: "danger",
      });
      if (!ok) {
        all.forEach((b) => b.removeAttribute("disabled")); return;
      }
      const second = await showPrompt({
        title: "Confirm by typing the name",
        body: `Type the skill name to confirm:`,
        sub: name,
        placeholder: name,
        confirmLabel: "Delete forever",
        confirmKind: "danger",
        validator: (v) => v === name,
      });
      if (second !== name) {
        toast("Hard delete cancelled");
        all.forEach((b) => b.removeAttribute("disabled")); return;
      }
      const resp = await fetch(`/api/skill/${encodeURIComponent(name)}/delete`, { method: "POST" });
      if (!resp.ok) throw new Error(`hard-delete failed: ${resp.status}`);
      toast(`Deleted ${name}`);
      // Close this pane.
      const pane = state.panes.find((p) => p.kind === "skill" && p.payload && p.payload.name === name);
      if (pane) closePane(pane.id);
      await loadAll();
    } else if (act === "bulk-delete-orphan") {
      const ok = await showConfirm({
        title: "Delete orphan verdicts",
        body: `Delete every verdict for orphan skill "${name}"?`,
        sub: "Cannot be undone.",
        confirmLabel: "Delete all",
        confirmKind: "danger",
      });
      if (!ok) {
        all.forEach((b) => b.removeAttribute("disabled")); return;
      }
      // Iterate the per-host buckets and issue one bulk-delete each.
      const byName = state.skillsByName.find((s) => s.name === name);
      const hosts = byName ? Object.keys(byName.per_host || {}) : ["other"];
      // skills_by_name normalises hosts; need raw values for the API.
      const rawByShort = {claude: ["claude_code", "claude"], gemini: ["gemini_cli", "gemini"], codex: ["codex"], hermes: ["hermes"], other: ["other"]};
      let total = 0;
      for (const h of hosts) {
        for (const raw of (rawByShort[h] || [h])) {
          const resp = await fetch("/api/verdicts/bulk-delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ skill_name: name, host: raw, reason: null }),
          });
          if (resp.ok) { const d = await resp.json(); total += d.deleted; }
          // Also reason-less variant: collect any non-null reasons
          // via /api/verdicts?skill=… then issue per-reason deletes.
          const reasons = await fetchJSON(`/api/verdicts?limit=200&skill=${encodeURIComponent(name)}`).catch(() => []);
          const reasonSet = [...new Set(reasons.filter((v) => v.host_raw === raw).map((v) => v.reason))];
          for (const r of reasonSet) {
            const dr = await fetch("/api/verdicts/bulk-delete", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ skill_name: name, host: raw, reason: r }),
            });
            if (dr.ok) { const d = await dr.json(); total += d.deleted; }
          }
        }
      }
      toast(`Deleted ${total} verdicts`);
      const pane = state.panes.find((p) => p.kind === "skill" && p.payload && p.payload.name === name);
      if (pane) closePane(pane.id);
      await loadAll();
    }
  } catch (err) {
    console.error(err); toast("Action failed");
    all.forEach((b) => b.removeAttribute("disabled"));
  }
}

async function openFolder(path) {
  try {
    const resp = await fetch("/api/open-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${resp.status}`);
    }
    toast("Opened in file manager");
  } catch (err) { console.error(err); toast(`Open failed: ${err.message}`); }
}

function drawMiniSpark(svg, data) {
  if (!svg || !data || data.length === 0) return;
  const ns = "http://www.w3.org/2000/svg";
  const counts = data.map(([, c]) => c);
  const max = Math.max(...counts, 1);
  const w = 300, h = 28, n = counts.length;
  const dx = w / Math.max(n - 1, 1);
  const points = counts.map((c, i) => `${(i * dx).toFixed(1)},${(h - (c / max) * (h - 2) - 1).toFixed(1)}`);
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", `M${points.join(" L")}`);
  path.setAttribute("fill", "none"); path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.5");
  svg.appendChild(path);
}

// ---------- State actions ---------- //

function toggleHost(host) {
  state.hostFilter = state.hostFilter === host ? null : host;
  state.orphanFilter = false;
  state.netHarmfulFilter = false;
  loadAll();
}
// `setSearch` removed — the old verdict-reason FTS path is gone.
// Use `setReviewFilter({field, value})` instead; it filters
// client-side and re-renders without a fetch.
function setReviewFilter(next) {
  state.reviewFilter = Object.assign({}, state.reviewFilter, next);
  // Filter changes invalidate stored page indices on every review
  // list, so jump back to page 0 in both modes.
  state.pages.skillsReview = 0;
  state.pages.verdictsReview = 0;
  renderAll();
}
function setListMode(m) {
  state.listMode = m;
  state.noiseFilter = false;
  state.orphanFilter = false;
  state.netHarmfulFilter = false;
  syncListModeButtons();
  renderMainList();
}

// ---------- Utilities ---------- //

function relTime(iso) {
  if (!iso) return "";
  const t = new Date(iso);
  const sec = Math.floor((Date.now() - t.getTime()) / 1000);
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h`;
  return `${Math.floor(sec / 86400)}d`;
}
function verdictGlyph(v) { return v === "HELPFUL" ? "✓" : v === "HARMFUL" ? "✗" : "·"; }
function truncate(s, max) {
  if (s.length <= max) return s;
  return s.slice(0, Math.max(1, max - 1)) + "…";
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }
function toast(msg) {
  let el = document.querySelector(".toast");
  if (!el) {
    el = document.createElement("div"); el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 1800);
}

// Promise-based modal confirm — styled to match the rest of the UI
// (reuses .modal + .modal-inner.confirm). Resolves true on confirm,
// false on cancel / Escape / backdrop click.
function showConfirm({ title = "Confirm", body = "", sub = "",
  confirmLabel = "Delete", confirmKind = "danger", cancelLabel = "Cancel" } = {}) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal";
    overlay.innerHTML = `
      <div class="modal-inner confirm" role="dialog" aria-modal="true">
        <header>
          <h2>${escapeHtml(title)}</h2>
          <button class="close" data-act="cancel" aria-label="Close">×</button>
        </header>
        <div class="confirm-body">
          <div>${escapeHtml(body)}</div>
          ${sub ? `<div class="sub">${escapeHtml(sub)}</div>` : ""}
        </div>
        <footer>
          <button class="action" data-act="cancel">${escapeHtml(cancelLabel)}</button>
          <button class="action ${confirmKind === "danger" ? "danger" : "primary"}" data-act="confirm">${escapeHtml(confirmLabel)}</button>
        </footer>
      </div>
    `;
    document.body.appendChild(overlay);
    const cleanup = (v) => {
      document.removeEventListener("keydown", onKey, true);
      overlay.remove();
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); cleanup(false); }
      else if (e.key === "Enter") { e.stopPropagation(); cleanup(true); }
    };
    document.addEventListener("keydown", onKey, true);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) cleanup(false);
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      cleanup(btn.dataset.act === "confirm");
    });
    // Focus the confirm button so Enter triggers it by default.
    requestAnimationFrame(() => {
      const cbtn = overlay.querySelector("[data-act='confirm']");
      if (cbtn) cbtn.focus();
    });
  });
}

// Promise-based modal prompt — same chrome as showConfirm but with an
// inline <input>. Resolves the entered string on confirm (or empty
// string if blank), null on cancel / Escape / backdrop click. Use
// `validator(value)` to gate the confirm button (returns true when
// the input is acceptable).
function showPrompt({ title = "Input", body = "", sub = "",
  placeholder = "", initial = "", confirmLabel = "OK",
  confirmKind = "primary", cancelLabel = "Cancel",
  validator = null } = {}) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal";
    overlay.innerHTML = `
      <div class="modal-inner confirm" role="dialog" aria-modal="true">
        <header>
          <h2>${escapeHtml(title)}</h2>
          <button class="close" data-act="cancel" aria-label="Close">×</button>
        </header>
        <div class="confirm-body">
          <div>${escapeHtml(body)}</div>
          ${sub ? `<div class="sub">${escapeHtml(sub)}</div>` : ""}
          <input class="confirm-input" type="text"
            placeholder="${escapeAttr(placeholder)}"
            value="${escapeAttr(initial)}" />
        </div>
        <footer>
          <button class="action" data-act="cancel">${escapeHtml(cancelLabel)}</button>
          <button class="action ${confirmKind === "danger" ? "danger" : "primary"}" data-act="confirm">${escapeHtml(confirmLabel)}</button>
        </footer>
      </div>
    `;
    document.body.appendChild(overlay);
    const input = overlay.querySelector(".confirm-input");
    const confirmBtn = overlay.querySelector("[data-act='confirm']");
    const validate = () => {
      const v = input.value;
      const ok = validator ? !!validator(v) : true;
      if (ok) confirmBtn.removeAttribute("disabled");
      else confirmBtn.setAttribute("disabled", "disabled");
      return ok;
    };
    input.addEventListener("input", validate);
    validate();
    const cleanup = (v) => {
      document.removeEventListener("keydown", onKey, true);
      overlay.remove();
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); cleanup(null); }
      else if (e.key === "Enter" && document.activeElement === input) {
        e.preventDefault();
        if (validate()) cleanup(input.value);
      }
    };
    document.addEventListener("keydown", onKey, true);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) { cleanup(null); return; }
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      if (btn.dataset.act === "confirm") {
        if (validate()) cleanup(input.value);
      } else cleanup(null);
    });
    requestAnimationFrame(() => input.focus());
  });
}

// ---------- Bootstrap ---------- //

function bootstrap() {
  // Tab strip — switching between the observability overview and the
  // HITL review surface. Applied before the first paint so the
  // restored localStorage tab is reflected from the very first frame.
  document.querySelectorAll("[data-tab]").forEach((b) => {
    b.addEventListener("click", () => setActiveTab(b.dataset.tab));
  });
  syncTabChrome();

  document.querySelectorAll("[data-list-mode]").forEach((b) => {
    b.addEventListener("click", () => setListMode(b.dataset.listMode));
  });
  syncListModeButtons();

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const active = document.activeElement;
    if (active && (active.tagName === "TEXTAREA" || active.tagName === "INPUT")) return;
    if (state.panes.length > 0) closePane(state.panes[state.panes.length - 1].id);
  });

  // Multi-category review filter (replaces the old reason FTS search).
  // - "title" / "description" → text input on the right
  // - "hosts" → dropdown of host shortnames
  // The two value-input widgets swap visibility based on category;
  // both feed back into `state.reviewFilter` via setReviewFilter().
  const filterField = document.getElementById("filter-field");
  const filterText = document.getElementById("filter-text");
  const filterHost = document.getElementById("filter-host");
  const filterClear = document.getElementById("filter-clear");
  const syncFilterUI = () => {
    const isHosts = state.reviewFilter.field === "hosts";
    filterText.hidden = isHosts;
    filterHost.hidden = !isHosts;
    const placeholders = {
      title: "match by skill name…",
      description: "match by description / reason…",
    };
    if (!isHosts) filterText.placeholder = placeholders[state.reviewFilter.field] || "search…";
    const hasValue = Boolean(state.reviewFilter.value);
    filterClear.hidden = !hasValue;
  };
  if (filterField) {
    filterField.value = state.reviewFilter.field;
    filterField.addEventListener("change", () => {
      // Reset the value when the category changes; comparing apples
      // to oranges would only confuse the visible result set.
      setReviewFilter({ field: filterField.value, value: "" });
      filterText.value = "";
      filterHost.value = "";
      syncFilterUI();
    });
  }
  if (filterText) {
    let textDebounce;
    filterText.addEventListener("input", () => {
      clearTimeout(textDebounce);
      textDebounce = setTimeout(() => {
        setReviewFilter({ value: filterText.value });
        syncFilterUI();
      }, 200);
    });
  }
  if (filterHost) {
    filterHost.addEventListener("change", () => {
      setReviewFilter({ value: filterHost.value });
      syncFilterUI();
    });
  }
  if (filterClear) {
    filterClear.addEventListener("click", () => {
      setReviewFilter({ value: "" });
      filterText.value = "";
      filterHost.value = "";
      syncFilterUI();
    });
  }
  syncFilterUI();

  loadAll();
  // Safety-gated poll tick:
  //   - Skip while the tab is hidden — no-one is reading the page.
  //   - Skip when a TEXTAREA / INPUT has focus (the user is mid-edit
  //     of a verdict reason or search query, and a re-render would
  //     either steal focus or stomp keystrokes between fetch and
  //     paint).
  // Both checks are O(1); when either trips we just wait for the
  // next tick so cadence stays predictable.
  const tick = () => {
    if (document.visibilityState !== "visible") return;
    const active = document.activeElement;
    if (active && (active.tagName === "TEXTAREA" || active.tagName === "INPUT")) {
      return;
    }
    loadAll();
  };
  pollTimer = setInterval(tick, POLL_MS);
  // Resume immediately when the tab becomes visible again so the
  // user doesn't see a stale snapshot for up to one POLL_MS after
  // switching back.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") tick();
  });

  // Auto-open a skill pane from URL hash — used by headless snapshot
  // tooling (and by the user when sharing a URL).
  // Example: http://127.0.0.1:7531/#open=api-route-handler
  //          http://127.0.0.1:7531/#open=imagegen&bucket=HELPFUL
  const applyHash = () => {
    const h = (location.hash || "").replace(/^#/, "");
    if (!h) return;
    const params = new URLSearchParams(h);
    const skill = params.get("open");
    const bucket = params.get("bucket");
    const expandId = params.get("expand");
    const composer = params.get("composer");
    if (skill) {
      openSkillPane(skill);
      const tick = setInterval(() => {
        const pane = state.panes[0];
        if (pane && pane.payload) {
          if (bucket) pane.expanded = bucket.toUpperCase();
          if (expandId) pane.expandedVerdictId = Number(expandId);
          if (composer === "1") pane.composerOpen = true;
          renderPanes();
          clearInterval(tick);
        }
      }, 100);
      setTimeout(() => clearInterval(tick), 4000);
    }
  };
  // Run after the first load finishes so the skill list is populated.
  const hashWatcher = setInterval(() => {
    if (state.skillsByName.length > 0 || state.overview) {
      applyHash();
      clearInterval(hashWatcher);
    }
  }, 100);
  setTimeout(() => clearInterval(hashWatcher), 5000);
  window.addEventListener("hashchange", applyHash);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootstrap);
} else {
  bootstrap();
}
