// Shared logic for index.html (map) and states.html (full table).
// Keeping this in one file means category colors/labels/descriptions only
// need to be edited in one place.

const CATEGORIES = {
  Permitted: {
    color: "var(--permitted)",
    desc: "A software or technology-based approach is explicitly workable under this state's law or guidance.",
  },
  Ambiguous: {
    color: "var(--ambiguous)",
    desc: "A restriction law exists but doesn't specify a method - could go either way until a district writes its own policy.",
  },
  "No Law": {
    color: "var(--nolaw)",
    desc: "No statewide legislation. Entirely up to local district discretion.",
  },
  Restricted: {
    color: "var(--restricted)",
    desc: "A hard, physical-storage mandate is in place - incompatible with a software-only approach.",
  },
};

function colorFor(c) {
  return (CATEGORIES[c] && CATEGORIES[c].color) || "#3a4258";
}
function pillClass(c) {
  return (c || "").toLowerCase().replace(/\s+/g, "-");
}

let DATA = [];
let META = {};
let CHANGELOG = [];
let byState = {};

async function loadData() {
  try {
    const res = await fetch("./data.json", { cache: "no-store" });
    if (!res.ok) throw new Error("data.json not found (status " + res.status + ")");
    return await res.json();
  } catch (e) {
    console.error("loadData failed:", e);
    const badge = document.getElementById("lastUpdated");
    if (badge) badge.textContent = "Could not load data.json - " + e.message;
    return { meta: {}, changelog: [], states: [] };
  }
}

async function initData() {
  const json = await loadData();
  DATA = json.states || [];
  META = json.meta || {};
  CHANGELOG = json.changelog || [];
  byState = {};
  DATA.forEach((d) => (byState[d.State] = d));

  if (META.lastUpdated) {
    const badge = document.getElementById("lastUpdated");
    if (badge) {
      badge.textContent = "Data current as of " + new Date(META.lastUpdated).toLocaleString();
      badge.classList.add("live");
    }
  }
}

// ---------------------------------------------------------------------------
// MODAL (full state detail)
// ---------------------------------------------------------------------------

function ensureModalMarkup() {
  if (document.getElementById("stateModal")) return;
  const div = document.createElement("div");
  div.innerHTML = `
    <div id="modalOverlay" class="modal-overlay">
      <div class="modal" id="stateModal" role="dialog" aria-modal="true">
        <button class="modal-close" id="modalClose" aria-label="Close">&times;</button>
        <div id="modalBody"></div>
      </div>
    </div>`;
  document.body.appendChild(div);
  document.getElementById("modalOverlay").addEventListener("click", (e) => {
    if (e.target.id === "modalOverlay") closeModal();
  });
  document.getElementById("modalClose").addEventListener("click", closeModal);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });
}

function renderBillSection(label, bills, kind) {
  if (!bills || !bills.length) return "";
  const cards = bills
    .map(
      (b) => `
    <div class="bill-card">
      <div class="row1"><span class="num">${b.bill || ""}</span><span class="status ${kind}">${b.progress || b.status || ""}</span></div>
      <div class="title">${b.title || ""}</div>
      <div class="implication">${b.implication || ""}</div>
      ${b.url ? `<a class="bill-link" href="${b.url}" target="_blank" rel="noopener">View bill →</a>` : ""}
    </div>`
    )
    .join("");
  return `<div class="section-label">${label}</div>${cards}`;
}

function openModal(stateName) {
  const d = byState[stateName];
  if (!d) return;
  ensureModalMarkup();
  const sources = (d.Sources || []).map((s) => `<a href="${s.url}" target="_blank" rel="noopener">${s.name}</a>`).join(" · ");
  const verifiedBadge = d.ManuallyVerified
    ? `<div class="verified-badge" title="${(d.VerificationNote || "").replace(/"/g, "&quot;")}">✓ Manually verified</div>`
    : "";
  const gapFillBadge = d.LegiScanGapFill
    ? `<div class="gapfill-badge" title="LegiScan detected this bill as passed before NCSL or Ballotpedia listed it. Worth a second look once those sources catch up.">⚠ Auto-detected via LegiScan</div>`
    : "";
  const reviewBadge = d.OverrideNeedsReview
    ? `<div class="review-badge" title="This state's classification was manually verified against a specific bill, but the current bill on record has changed since then - the legislation may have moved. The manual conclusion is still applied, but worth a fresh look.">⚠ Override may be outdated - recheck</div>`
    : "";
  document.getElementById("modalBody").innerHTML = `
    <div class="detail-header">
      <h3>${d.State}</h3>
      <span class="pill ${pillClass(d.DoormanCompatibility)}">${d.DoormanCompatibility || "Unknown"}</span>
    </div>
    ${verifiedBadge}${gapFillBadge}${reviewBadge}
    <div class="field"><div class="k">Legislation Status</div><div class="v">${d.LegislationStatus || "—"}</div></div>
    <div class="field"><div class="k">Bill(s)</div><div class="v">${d.BillNumbers || "—"}</div></div>
    <div class="field"><div class="k">Ban Type</div><div class="v">${d.BanType || "—"}</div></div>
    <div class="field"><div class="k">Funding</div><div class="v">${d.Funding || "—"}</div></div>
    <div class="field"><div class="k">Compliance Guidance Provided</div><div class="v">${d.ComplianceGuidance || "—"}</div></div>
    <div class="field"><div class="k">Details</div><div class="details-text">${(d.Details || "").replace(/</g, "&lt;")}</div></div>
    ${d.VerificationNote ? `<div class="field"><div class="k">Verification Note</div><div class="details-text">${d.VerificationNote.replace(/</g, "&lt;")}</div></div>` : ""}
    <div class="field"><div class="k">Sources</div><div class="v">${sources || "—"} <span class="muted">(checked ${d.LastChecked || "—"})</span></div></div>
    ${renderBillSection("In progress", d.InProgress, "inprogress")}
    ${renderBillSection("Failed / did not pass", d.Failed, "failed")}
  `;
  document.getElementById("modalOverlay").classList.add("open");
  document.body.style.overflow = "hidden";
}

function closeModal() {
  const overlay = document.getElementById("modalOverlay");
  if (overlay) overlay.classList.remove("open");
  document.body.style.overflow = "";
}

// ---------------------------------------------------------------------------
// HOVER TOOLTIP (basic info)
// ---------------------------------------------------------------------------

function ensureTooltipMarkup() {
  if (document.getElementById("stateTooltip")) return;
  const div = document.createElement("div");
  div.id = "stateTooltip";
  div.className = "state-tooltip";
  document.body.appendChild(div);
}

function showTooltip(stateName, x, y) {
  const d = byState[stateName];
  if (!d) return;
  ensureTooltipMarkup();
  const tip = document.getElementById("stateTooltip");
  tip.innerHTML = `
    <div class="tt-name">${d.State}</div>
    <span class="pill ${pillClass(d.DoormanCompatibility)}">${d.DoormanCompatibility || "Unknown"}</span>
    <div class="tt-desc">${(CATEGORIES[d.DoormanCompatibility] || {}).desc || ""}</div>
    <div class="tt-more">Click for full details →</div>
  `;
  tip.style.display = "block";
  positionTooltip(x, y);
}

function positionTooltip(x, y) {
  const tip = document.getElementById("stateTooltip");
  if (!tip) return;
  const pad = 14;
  let left = x + pad;
  let top = y + pad;
  const rect = tip.getBoundingClientRect();
  if (left + rect.width > window.innerWidth) left = x - rect.width - pad;
  if (top + rect.height > window.innerHeight) top = y - rect.height - pad;
  tip.style.left = left + "px";
  tip.style.top = top + "px";
}

function hideTooltip() {
  const tip = document.getElementById("stateTooltip");
  if (tip) tip.style.display = "none";
}

// ---------------------------------------------------------------------------
// SEARCH AUTOCOMPLETE (attach to #search / #searchResults if present)
// ---------------------------------------------------------------------------

function wireSearchAutocomplete() {
  const input = document.getElementById("search");
  const results = document.getElementById("searchResults");
  if (!input || !results) return;

  function renderResults(matches) {
    results.innerHTML = "";
    if (!matches.length) {
      results.style.display = "none";
      return;
    }
    matches.slice(0, 8).forEach((d) => {
      const item = document.createElement("div");
      item.className = "search-result";
      item.innerHTML = `<span>${d.State}</span><span class="pill ${pillClass(d.DoormanCompatibility)}">${d.DoormanCompatibility}</span>`;
      item.addEventListener("mousedown", (e) => {
        e.preventDefault();
        openModal(d.State);
        input.value = "";
        results.style.display = "none";
      });
      results.appendChild(item);
    });
    results.style.display = "block";
  }

  input.addEventListener("input", () => {
    const v = input.value.trim().toLowerCase();
    if (!v) {
      results.style.display = "none";
      return;
    }
    const matches = DATA.filter((d) => d.State.toLowerCase().includes(v)).sort((a, b) => {
      const av = a.State.toLowerCase().indexOf(v);
      const bv = b.State.toLowerCase().indexOf(v);
      return av - bv || a.State.localeCompare(b.State);
    });
    renderResults(matches);
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      const v = input.value.trim().toLowerCase();
      if (!v) return;
      const match = DATA.filter((d) => d.State.toLowerCase().includes(v)).sort((a, b) => {
        const av = a.State.toLowerCase().indexOf(v);
        const bv = b.State.toLowerCase().indexOf(v);
        return av - bv || a.State.localeCompare(b.State);
      })[0];
      if (match) {
        openModal(match.State);
        input.value = "";
        results.style.display = "none";
      }
    } else if (e.key === "Escape") {
      results.style.display = "none";
      input.blur();
    }
  });

  document.addEventListener("click", (e) => {
    if (e.target !== input) results.style.display = "none";
  });
}

