const apiBase = window.origin || `${window.location.protocol}//${window.location.host}`;

const state = {
  apiBase: apiBase,
  limit: 100,
  offset: 0,
  sourceConversationId: "",
  sourceSessionId: "",
  importId: "",
  truthfulOnly: false,
  selectedEntryId: null,
  searchQuery: "",
  selectedSearchHitEntryId: null,
  openedFromSearchHit: null,
};
const STORAGE_KEY = "agentDiaryUiStateV2";

/** Escape HTML special characters to prevent XSS */
function esc(str) {
  const div = document.createElement("div");
  div.appendChild(document.createTextNode(String(str ?? "")));
  return div.innerHTML;
}

const apiBaseInput = document.getElementById("apiBase");
const reloadBtn = document.getElementById("reloadBtn");
const statusDot = document.getElementById("statusDot");
const prevPageBtn = document.getElementById("prevPageBtn");
const nextPageBtn = document.getElementById("nextPageBtn");
const refreshImportsBtn = document.getElementById("refreshImportsBtn");
const scopeBar = document.getElementById("scopeBar");
const timelineList = document.getElementById("timelineList");
const timelineStatus = document.getElementById("timelineStatus");
const importsStatus = document.getElementById("importsStatus");
const importsList = document.getElementById("importsList");
const recallBanner = document.getElementById("recallBanner");
const detailMeta = document.getElementById("detailMeta");
const detailBody = document.getElementById("detailBody");
const detailStatus = document.getElementById("detailStatus");
const artifactStatusBar = document.getElementById("artifactStatusBar");
const interpHeader = document.getElementById("interpHeader");
const loopDetails = document.getElementById("loopDetails");
const loopList = document.getElementById("loopList");
const briefDetails = document.getElementById("briefDetails");
const briefBody = document.getElementById("briefBody");
const memoryDetails = document.getElementById("memoryDetails");
const memoryArtifactList = document.getElementById("memoryArtifactList");
const workTraceDetails = document.getElementById("workTraceDetails");
const workTraceList = document.getElementById("workTraceList");
const overlayDetails = document.getElementById("overlayDetails");
const overlayList = document.getElementById("overlayList");
const overlayForm = document.getElementById("overlayForm");
const overlayTypeInput = document.getElementById("overlayType");
const overlayAuthorInput = document.getElementById("overlayAuthor");
const overlayContentInput = document.getElementById("overlayContent");
const overlayStatus = document.getElementById("overlayStatus");
const refreshDerivedDetails = document.getElementById("refreshDerivedDetails");
const refreshOpenLoopsBtn = document.getElementById("refreshOpenLoopsBtn");
const refreshBriefsBtn = document.getElementById("refreshBriefsBtn");
const refreshMemoryBtn = document.getElementById("refreshMemoryBtn");
const refreshDerivedStatus = document.getElementById("refreshDerivedStatus");
const refreshDerivedScope = document.getElementById("refreshDerivedScope");
const artifactList = document.getElementById("artifactList");
const artifactDetails = document.getElementById("artifactDetails");
const searchForm = document.getElementById("searchForm");
const searchInput = document.getElementById("searchInput");
const searchResults = document.getElementById("searchResults");
const searchStatus = document.getElementById("searchStatus");
const browseScopeForm = document.getElementById("browseScopeForm");
const scopeConversationIdInput = document.getElementById("scopeConversationId");
const scopeImportIdInput = document.getElementById("scopeImportId");
const scopeTruthfulOnlyInput = document.getElementById("scopeTruthfulOnly");
const clearScopeBtn = document.getElementById("clearScopeBtn");
let isApplyingUrlState = false;
let selectedTimelineContext = null;

const SPEAKER_TONES = [
  {
    accent: "#2f5d78",
    surface: "#eaf3f8",
    ink: "#123041",
    border: "#b8cede",
  },
  {
    accent: "#7a4f1d",
    surface: "#f6ead8",
    ink: "#4a3016",
    border: "#dcb98a",
  },
  {
    accent: "#4f6d2d",
    surface: "#edf4e3",
    ink: "#2a4017",
    border: "#bfd09f",
  },
  {
    accent: "#6b467d",
    surface: "#f1e8f7",
    ink: "#382246",
    border: "#d3bedf",
  },
  {
    accent: "#8b3f59",
    surface: "#f8e9ef",
    ink: "#4d2130",
    border: "#e0b7c6",
  },
  {
    accent: "#2f6d68",
    surface: "#e8f4f2",
    ink: "#153935",
    border: "#b4d6d1",
  },
];

async function post(path, payload) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(`${state.apiBase}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    clearTimeout(timeoutId);
    const data = await response.json();
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    return data.result || data;
  } catch (err) {
    clearTimeout(timeoutId);
    throw err;
  }
}

async function checkConnection() {
  const url = state.apiBase;
  if (!url) { statusDot.className = "status-dot status-unknown"; return; }
  statusDot.className = "status-dot status-unknown";
  try {
    const response = await fetch(`${url}/status`, { method: "GET", signal: AbortSignal.timeout(3000) });
    if (response.ok) {
      statusDot.className = "status-dot status-connected";
      statusDot.title = "Connected to server";
    } else {
      statusDot.className = "status-dot status-error";
      statusDot.title = `Server error: ${response.status}`;
    }
  } catch {
    statusDot.className = "status-dot status-error";
    statusDot.title = "Cannot reach server";
  }
}

function renderTimeline(items) {
  timelineList.setAttribute("aria-busy", "false");
  timelineList.innerHTML = "";
  if (!items.length) {
    timelineList.innerHTML = '<li class="item muted">No entries in this page.</li>';
    return;
  }

  const grouped = groupByDay(items);
  for (const [dayKey, dayItems] of grouped) {
    const headerLi = document.createElement("li");
    headerLi.className = "day-header";
    headerLi.textContent = formatDayHeader(dayKey);
    timelineList.appendChild(headerLi);

    for (const item of dayItems) {
      timelineList.appendChild(buildTimelineItem(item));
    }
  }

  if (
    selectedTimelineContext &&
    state.selectedEntryId === selectedTimelineContext.entry_id &&
    !items.some((item) => item.entry_id === state.selectedEntryId)
  ) {
    const headerLi = document.createElement("li");
    headerLi.className = "day-header";
    headerLi.textContent = "Selected Entry";
    timelineList.prepend(buildTimelineItem(selectedTimelineContext));
    timelineList.prepend(headerLi);
  }
  wireListKeyboardNav(timelineList);
}

function buildTimelineItem(item) {
  const li = document.createElement("li");
  li.className = "item";
  const btn = document.createElement("button");
  btn.setAttribute("data-entry-id", item.entry_id);
  btn.setAttribute("aria-current", state.selectedEntryId === item.entry_id ? "true" : "false");
  if (state.selectedEntryId === item.entry_id) btn.className = "active";

  const meta = document.createElement("div");
  meta.className = "muted";
  meta.textContent = `${formatEntryTime(item.created_at)} · ${item.entry_type} · ${item.source} · ${item.author_role}`;
  btn.appendChild(meta);

  // Work trace indicator badge
  if (item.work_trace_count > 0) {
    const wtBadge = document.createElement("span");
    wtBadge.className = "wt-badge";
    wtBadge.textContent = `${item.work_trace_count} work`;
    wtBadge.title = `${item.work_trace_count} agent work event(s)`;
    meta.appendChild(wtBadge);
  }

  if (item.brief) {
    const brief = document.createElement("div");
    brief.className = "brief";
    brief.textContent = item.brief;
    btn.appendChild(brief);
  }

  const preview = document.createElement("div");
  preview.className = "preview";
  preview.textContent = item.preview;
  btn.appendChild(preview);

  btn.addEventListener("click", () => loadEntry(item.entry_id));
  li.appendChild(btn);
  return li;
}

function groupByDay(items) {
  const map = new Map();
  for (const item of items) {
    const key = normalizeDayKey(item.created_at);
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(item);
  }
  return map;
}

function normalizeDayKey(iso) {
  const date = new Date(iso);
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function formatDayHeader(dayKey) {
  const day = new Date(`${dayKey}T00:00:00`);
  const todayKey = normalizeDayKey(new Date().toISOString());
  const yesterdayDate = new Date();
  yesterdayDate.setDate(yesterdayDate.getDate() - 1);
  const yesterdayKey = normalizeDayKey(yesterdayDate.toISOString());

  const base = day.toLocaleDateString(undefined, {
    weekday: "long",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
  if (dayKey === todayKey) return `Today · ${base}`;
  if (dayKey === yesterdayKey) return `Yesterday · ${base}`;
  return base;
}

function formatEntryTime(iso) {
  const date = new Date(iso);
  const human = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return `${human} (${iso})`;
}

function formatMetaDateTime(iso) {
  const date = new Date(iso);
  return `${date.toLocaleString()} · ${iso}`;
}

function appendMutedLabelValue(parent, label, value) {
  const row = document.createElement("div");
  row.className = "muted";
  const strong = document.createElement("strong");
  strong.textContent = label + ":";
  row.appendChild(strong);
  row.append(" " + String(value ?? ""));
  parent.appendChild(row);
  return row;
}

function attachSupportLinkHandler(button) {
  button.addEventListener("click", async () => {
    const id = button.getAttribute("data-support-entry-id");
    if (!id) return;
    await loadEntry(id);
  });
}

function createSourceEntryLinks(sourceEntryIds) {
  const wrap = document.createElement("span");
  wrap.className = "support-links";
  const ids = Array.isArray(sourceEntryIds) ? sourceEntryIds.filter((id) => String(id || "").trim()) : [];
  if (!ids.length) {
    const none = document.createElement("span");
    none.className = "muted";
    none.textContent = "none";
    wrap.appendChild(none);
    return wrap;
  }
  for (const id of ids) {
    const normalizedId = String(id);
    const button = document.createElement("button");
    button.className = "support-link";
    button.type = "button";
    button.setAttribute("data-support-entry-id", normalizedId);
    button.textContent = normalizedId;
    attachSupportLinkHandler(button);
    wrap.appendChild(button);
    wrap.append(" ");
  }
  return wrap;
}

function createOverlayStalenessBlock(artifact) {
  if (!artifact || artifact.overlay_stale !== true) {
    return null;
  }
  const fragment = document.createDocumentFragment();
  const badge = document.createElement("div");
  badge.className = "stale-badge";
  badge.setAttribute("role", "note");
  badge.setAttribute("aria-label", "Artifact may be stale after overlay");
  badge.textContent = "May be stale after overlay";
  fragment.appendChild(badge);

  const generatedAt = artifact.artifact_generated_at
    ? formatMetaDateTime(artifact.artifact_generated_at)
    : "unknown";
  const overlayAt = artifact.latest_overlay_at ? formatMetaDateTime(artifact.latest_overlay_at) : "unknown";
  const generated = document.createElement("div");
  generated.className = "muted stale-meta";
  generated.textContent = "artifact generated: " + generatedAt;
  fragment.appendChild(generated);
  const overlay = document.createElement("div");
  overlay.className = "muted stale-meta";
  overlay.textContent = "latest overlay: " + overlayAt;
  fragment.appendChild(overlay);
  return fragment;
}

function createProvenanceBlock(artifact) {
  const p = artifact?.provenance || {};
  const block = document.createElement("div");
  block.className = "provenance-block";

  const badge = document.createElement("div");
  badge.className = "derived-badge";
  badge.textContent = "Provenance";
  block.appendChild(badge);

  if (p.schema_version) appendMutedLabelValue(block, "schema", p.schema_version);
  if (p.method) appendMutedLabelValue(block, "method", p.method);
  if (p.method_version) appendMutedLabelValue(block, "method version", p.method_version);
  if (p.generated_at) appendMutedLabelValue(block, "generated", formatMetaDateTime(p.generated_at));
  if (p.analysis_window && (p.analysis_window.start || p.analysis_window.end)) {
    appendMutedLabelValue(block, "window", `${p.analysis_window.start || "?"} → ${p.analysis_window.end || "?"}`);
  }

  const sourceRow = document.createElement("div");
  sourceRow.className = "muted";
  const label = document.createElement("strong");
  label.textContent = "source entries:";
  sourceRow.appendChild(label);
  sourceRow.append(" ");
  sourceRow.appendChild(createSourceEntryLinks(p.source_entry_ids));
  block.appendChild(sourceRow);
  return block;
}

function normalizeSpeakerLabel(label) {
  return String(label || "")
    .trim()
    .replace(/\s+/g, " ");
}

function speakerTone(label) {
  const normalized = normalizeSpeakerLabel(label).toLowerCase();
  if (!normalized) {
    return SPEAKER_TONES[0];
  }
  if (["user", "human", "you", "sampleuser"].includes(normalized)) {
    return SPEAKER_TONES[0];
  }
  if (["assistant", "agent", "assistant", "codex", "bot"].includes(normalized)) {
    return SPEAKER_TONES[1];
  }
  let hash = 0;
  for (let index = 0; index < normalized.length; index += 1) {
    hash = (hash * 31 + normalized.charCodeAt(index)) >>> 0;
  }
  return SPEAKER_TONES[hash % SPEAKER_TONES.length];
}

function parseDialogueTurns(content) {
  if (typeof content !== "string" || !content.trim()) {
    return [];
  }

  const turns = [];
  let current = null;
  for (const line of content.replace(/\r\n/g, "\n").split("\n")) {
    const match = line.match(/^([A-Za-z][A-Za-z0-9._/-]{0,40}):\s*(.*)$/);
    if (match) {
      if (current) {
        turns.push(current);
      }
      current = {
        speaker: normalizeSpeakerLabel(match[1]),
        body: match[2],
      };
      continue;
    }
    if (!current) {
      current = { speaker: "", body: line };
      continue;
    }
    current.body = current.body ? `${current.body}\n${line}` : line;
  }
  if (current) {
    turns.push(current);
  }
  return turns.filter((turn) => turn.body.trim() || turn.speaker.trim());
}

function clearDetailBody() {
  detailBody.classList.remove("dialogue-view");
  detailBody.innerHTML = "";
}

function renderDialogueBody(raw, turns) {
  detailBody.classList.add("dialogue-view");
  detailBody.innerHTML = "";

  const transcript = document.createElement("div");
  transcript.className = "dialogue-transcript";

  const note = document.createElement("div");
  note.className = "dialogue-note muted";
  note.textContent = "Source record view. This stored entry is the evidence; summaries are only helpers.";
  transcript.appendChild(note);

  const renderedTurns = turns.length ? turns : [{ speaker: raw.speaker || "", body: raw.content || "" }];
  for (const turn of renderedTurns) {
    const speakerLabel = normalizeSpeakerLabel(turn.speaker) || "Speaker";
    const tone = speakerTone(speakerLabel);
    const card = document.createElement("article");
    card.className = "dialogue-turn";
    card.style.setProperty("--turn-accent", tone.accent);
    card.style.setProperty("--turn-surface", tone.surface);
    card.style.setProperty("--turn-ink", tone.ink);
    card.style.setProperty("--turn-border", tone.border);

    const header = document.createElement("div");
    header.className = "dialogue-turn-header";

    const speaker = document.createElement("span");
    speaker.className = "dialogue-speaker";
    speaker.textContent = speakerLabel;

    const body = document.createElement("div");
    body.className = "dialogue-turn-body";
    body.textContent = turn.body || "";

    header.appendChild(speaker);
    card.appendChild(header);
    card.appendChild(body);
    transcript.appendChild(card);
  }

  detailBody.appendChild(transcript);
}

function renderLoops(loopArtifacts) {
  loopList.innerHTML = "";
  if (!loopArtifacts.length) {
    loopList.innerHTML = '<li class="item muted">No possible follow-ups have been generated for this entry yet.</li>';
    return;
  }
  for (const artifact of loopArtifacts) {
    const li = document.createElement("li");
    li.className = "item";
    const loops = Array.isArray(artifact.open_loops) ? artifact.open_loops : [];

    // Header
    const headDiv = document.createElement("div");
    const headStrong = document.createElement("strong");
    headStrong.textContent = "Possible follow-ups";
    headDiv.appendChild(headStrong);
    li.appendChild(headDiv);

    // Meta
    function addMeta(text) {
      const d = document.createElement("div");
      d.className = "muted";
      d.textContent = text;
      li.appendChild(d);
    }
    addMeta(formatMetaDateTime(artifact.created_at || ""));
    addMeta("producer: " + (artifact.producer || ""));
    addMeta("status: " + (artifact.lifecycle_status || "active") + (artifact.is_current ? " · current" : ""));

    li.appendChild(createProvenanceBlock(artifact));
    const staleBlock = createOverlayStalenessBlock(artifact);
    if (staleBlock) li.appendChild(staleBlock);

    const badge = document.createElement("div");
    badge.className = "derived-badge";
    badge.textContent = "Generated helper";
    li.appendChild(badge);

    addMeta("Possible follow-ups: " + loops.length);

    const ul = document.createElement("ul");
    ul.className = "loop-list";
    for (const loop of loops) {
      const itemLi = document.createElement("li");
      itemLi.className = "loop-item";

      const titleDiv = document.createElement("div");
      const titleStrong = document.createElement("strong");
      titleStrong.textContent = loop.title || "Open loop";
      titleDiv.appendChild(titleStrong);
      itemLi.appendChild(titleDiv);

      if (loop.summary) {
        const sumDiv = document.createElement("div");
        sumDiv.className = "muted";
        sumDiv.textContent = loop.summary;
        itemLi.appendChild(sumDiv);
      }

      const strength = loop?.signals?.strength || "unknown";
      const confidence = typeof loop?.signals?.confidence === "number"
        ? " (" + Math.round(loop.signals.confidence * 100) + "%)" : "";
      const sigDiv = document.createElement("div");
      sigDiv.className = "muted";
      sigDiv.textContent = "strength: " + strength + confidence;
      itemLi.appendChild(sigDiv);

      const suppLabel = document.createElement("div");
      suppLabel.className = "muted";
      suppLabel.textContent = "supporting entries:";
      itemLi.appendChild(suppLabel);

      const linksDiv = document.createElement("div");
      linksDiv.className = "support-links";
      if (Array.isArray(loop.supporting_entry_ids) && loop.supporting_entry_ids.length) {
        for (const id of loop.supporting_entry_ids) {
          const btn = document.createElement("button");
          btn.className = "support-link";
          btn.type = "button";
          btn.setAttribute("data-support-entry-id", id);
          btn.textContent = id;
          btn.addEventListener("click", async () => {
            if (id) await loadEntry(id);
          });
          linksDiv.appendChild(btn);
        }
      } else {
        const noneSpan = document.createElement("span");
        noneSpan.className = "muted";
        noneSpan.textContent = "none";
        linksDiv.appendChild(noneSpan);
      }
      itemLi.appendChild(linksDiv);
      ul.appendChild(itemLi);
    }
    li.appendChild(ul);
    loopList.appendChild(li);
  }
}

function renderWorkTrace(workTraceEvents) {
  workTraceList.innerHTML = "";
  if (!workTraceEvents.length) {
    workTraceList.innerHTML = '<li class="item muted">No recorded agent work is attached to this entry yet.</li>';
    workTraceDetails.open = false;
    return;
  }
  for (const event of workTraceEvents) {
    const li = document.createElement("li");
    li.className = "item";

    const card = document.createElement("div");
    card.className = "work-trace-card";

    const title = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = event.summary || event.event_type || "Work event";
    title.appendChild(strong);
    card.appendChild(title);

    const meta = document.createElement("div");
    meta.className = "work-trace-meta";
    for (const value of [
      event.event_type,
      event.actor,
      event.project,
      event.source_surface,
      event.task_id ? `task ${event.task_id}` : "",
      event.session_key ? `session ${event.session_key}` : "",
    ]) {
      if (!value) continue;
      const pill = document.createElement("span");
      pill.className = "work-trace-pill";
      pill.textContent = value;
      meta.appendChild(pill);
    }
    if (meta.childElementCount) {
      card.appendChild(meta);
    }

    const when = document.createElement("div");
    when.className = "muted";
    when.textContent = formatMetaDateTime(event.created_at || "");
    card.appendChild(when);

    if (Array.isArray(event.related_paths) && event.related_paths.length) {
      const pathsLabel = document.createElement("p");
      pathsLabel.className = "work-trace-subhead";
      pathsLabel.textContent = "Touched Paths";
      card.appendChild(pathsLabel);

      const pathWrap = document.createElement("div");
      pathWrap.className = "work-trace-links";
      for (const path of event.related_paths) {
        const pill = document.createElement("span");
        pill.className = "work-trace-pill";
        pill.textContent = path;
        pathWrap.appendChild(pill);
      }
      card.appendChild(pathWrap);
    }

    if (Array.isArray(event.related_artifact_ids) && event.related_artifact_ids.length) {
      const artifactsLabel = document.createElement("p");
      artifactsLabel.className = "work-trace-subhead";
      artifactsLabel.textContent = "Related Artifacts";
      card.appendChild(artifactsLabel);

      const artifactWrap = document.createElement("div");
      artifactWrap.className = "work-trace-links";
      for (const artifactId of event.related_artifact_ids) {
        const pill = document.createElement("span");
        pill.className = "work-trace-pill";
        pill.textContent = artifactId;
        artifactWrap.appendChild(pill);
      }
      card.appendChild(artifactWrap);
    }

    if (event.details && Object.keys(event.details).length) {
      const detailsPre = document.createElement("pre");
      detailsPre.className = "artifact-body";
      detailsPre.textContent = JSON.stringify(event.details, null, 2);
      card.appendChild(detailsPre);
    }

    li.appendChild(card);
    workTraceList.appendChild(li);
  }
  workTraceDetails.open = true;
}

function renderArtifactStatusBar(briefArtifacts, loopArtifacts, overlays, memoryArtifacts, workTraceEvents) {
  artifactStatusBar.innerHTML = "";
  const hasStale = [...briefArtifacts, ...loopArtifacts, ...memoryArtifacts].some(
    (artifact) => artifact?.overlay_stale === true
  );

  function appendPill(text, extraClass = "") {
    const pill = document.createElement("span");
    pill.className = extraClass ? `status-pill ${extraClass}` : "status-pill";
    pill.textContent = text;
    artifactStatusBar.appendChild(pill);
  }

  appendPill("Summary: " + (briefArtifacts.length ? "✓" : "none"));
  if (loopArtifacts.length) {
    const count = loopArtifacts.reduce((sum, artifact) => {
      const loops = Array.isArray(artifact.open_loops) ? artifact.open_loops.length : 0;
      return sum + loops;
    }, 0);
    appendPill("Follow-ups: " + count);
  }
  if (overlays.length) {
    appendPill("Notes: " + overlays.length);
  }
  if (workTraceEvents.length) {
    appendPill("Work: " + workTraceEvents.length);
  }
  if (hasStale) {
    appendPill("STALE", "status-pill-stale");
  }
}

function renderInterpHeader(briefArtifacts, loopArtifacts, memoryArtifacts, workTraceEvents) {
  const allArtifacts = [...briefArtifacts, ...loopArtifacts, ...memoryArtifacts];
  const staleCount = allArtifacts.filter((artifact) => artifact?.overlay_stale === true).length;
  if (!allArtifacts.length && !workTraceEvents.length) {
    interpHeader.textContent = "";
    interpHeader.classList.remove("has-stale");
    return;
  }
  const pieces = [];
  if (allArtifacts.length) {
    pieces.push(allArtifacts.length + " artifacts");
  }
  if (workTraceEvents.length) {
    pieces.push(workTraceEvents.length + " work events");
  }
  if (staleCount) {
    pieces.push("⚠ " + staleCount + " may be stale");
  }
  interpHeader.textContent = pieces.join(" · ");
  interpHeader.classList.toggle("has-stale", staleCount > 0);
}

function renderScopeBar() {
  const pills = [];
  if (state.sourceConversationId) {
    pills.push({ label: "conversation: " + state.sourceConversationId, cls: "scope-pill" });
  }
  if (state.importId) {
    pills.push({ label: "import: " + state.importId, cls: "scope-pill" });
  }
  if (state.truthfulOnly) {
    pills.push({ label: "truthful only", cls: "scope-pill" });
  }
  scopeBar.innerHTML = "";
  if (!pills.length) return;
  for (const p of pills) {
    const span = document.createElement("span");
    span.className = p.cls;
    span.textContent = p.label;
    scopeBar.appendChild(span);
  }
  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "scope-clear-btn";
  clearBtn.setAttribute("data-clear-scope", "true");
  clearBtn.textContent = "Clear scope ×";
  clearBtn.addEventListener("click", async () => { await clearScopeState(); });
  scopeBar.appendChild(clearBtn);
}

function renderRecallBanner(hit) {
  if (!hit) {
    clearRecallBanner();
    return;
  }
  const layer = hit.match_layer === "compressed_memory" ? "generated search memory" : "source record";
  recallBanner.hidden = false;
  recallBanner.innerHTML = "";
  const bannerDiv = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = "Found in " + layer + ": ";
  bannerDiv.appendChild(strong);
  bannerDiv.append('"' + (hit.match_text || "") + '"');
  recallBanner.appendChild(bannerDiv);
  const dismissBtn = document.createElement("button");
  dismissBtn.type = "button";
  dismissBtn.className = "scope-clear-btn";
  dismissBtn.setAttribute("data-dismiss-recall", "true");
  dismissBtn.setAttribute("aria-label", "Dismiss recall context");
  dismissBtn.textContent = "×";
  dismissBtn.addEventListener("click", () => { clearRecallBanner(); });
  recallBanner.appendChild(dismissBtn);
}

function clearRecallBanner() {
  recallBanner.hidden = true;
  recallBanner.innerHTML = "";
  state.openedFromSearchHit = null;
}

function describeRefreshScope(payload) {
  if (payload.entry_ids?.length) {
    return "Scope: selected entry";
  }
  const parts = [];
  if (payload.source_conversation_id) parts.push("conversation=" + payload.source_conversation_id);
  if (payload.source_session_id) parts.push("session=" + payload.source_session_id);
  if (payload.import_id) parts.push("import=" + payload.import_id);
  if (payload.truthful_only) parts.push("truthful-only");
  return parts.length ? "Scope: " + parts.join(", ") : "Scope: none selected";
}

async function clearScopeState() {
  scopeConversationIdInput.value = "";
  scopeImportIdInput.value = "";
  scopeTruthfulOnlyInput.checked = false;
  state.sourceConversationId = "";
  state.sourceSessionId = "";
  state.importId = "";
  state.truthfulOnly = false;
  state.offset = 0;
  persistState();
  writeUrlState();
  renderScopeBar();
  await loadTimeline();
  await loadImports();
}

function renderDetail(detail) {
  const raw = detail.raw_entry;
  const overlays = Array.isArray(detail.overlays) ? detail.overlays : [];
  const artifacts = Array.isArray(detail.artifacts) ? detail.artifacts : [];
  const workTraceEvents = Array.isArray(detail.work_trace?.events) ? detail.work_trace.events : [];
  artifactStatusBar.innerHTML = "";
  const memoryArtifacts = artifacts.filter((artifact) =>
    ["memory", "compressed-memory"].includes(artifact.artifact_type)
  );
  const briefArtifacts = artifacts.filter((artifact) => artifact.artifact_type === "conversation-brief");
  const loopArtifacts = artifacts.filter((artifact) => artifact.artifact_type === "analysis:open-loop");
  const secondaryArtifacts = artifacts.filter(
    (artifact) =>
      !["memory", "compressed-memory", "conversation-brief", "analysis:open-loop"].includes(artifact.artifact_type)
  );
  detailMeta.innerHTML = "";
  for (const text of [formatMetaDateTime(raw.created_at), raw.entry_type, raw.source, raw.author_role]) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = text || "";
    detailMeta.appendChild(chip);
  }

  const content = raw.content || "";
  const turns = raw.entry_type === "chat_log" && raw.author_role === "mixed" ? parseDialogueTurns(content) : [];
  if (turns.length) {
    renderDialogueBody(raw, turns);
  } else {
    clearDetailBody();
    detailBody.textContent = content;
  }
  detailBody.scrollTop = 0;
  detailStatus.textContent = `Viewing source record ${raw.entry_id}`;
  detailBody.setAttribute("aria-label", `Diary entry ${raw.entry_id}`);

  if (briefArtifacts.length) {
    const currentBrief =
      briefArtifacts.find((artifact) => artifact.is_current) || briefArtifacts[0];
    briefBody.innerHTML = "";
    const content = document.createElement("pre");
    content.className = "artifact-body";
    content.textContent = currentBrief.content || "";
    briefBody.appendChild(content);
    briefBody.appendChild(createProvenanceBlock(currentBrief));
    const staleBlock = createOverlayStalenessBlock(currentBrief);
    if (staleBlock) briefBody.appendChild(staleBlock);
    briefDetails.open = true;
  } else {
    briefBody.textContent = "No plain-language summary is attached to this entry yet.";
    briefDetails.open = false;
  }

  memoryArtifactList.innerHTML = "";
  if (!memoryArtifacts.length) {
    memoryArtifactList.innerHTML =
      '<li class="item muted">No search-memory artifact is attached to this entry yet.</li>';
  }
  for (const artifact of memoryArtifacts) {
    const li = document.createElement("li");
    li.className = "item";

    const headDiv = document.createElement("div");
    const headStrong = document.createElement("strong");
    headStrong.textContent = artifact.artifact_type || "compressed-memory";
    headDiv.appendChild(headStrong);
    li.appendChild(headDiv);

    const meta = document.createElement("div");
    meta.className = "muted";
    meta.textContent = formatMetaDateTime(artifact.created_at || "");
    li.appendChild(meta);

    const prod = document.createElement("div");
    prod.className = "muted";
    prod.textContent = "producer: " + (artifact.producer || "");
    li.appendChild(prod);

    li.appendChild(createProvenanceBlock(artifact));
    const staleBlock = createOverlayStalenessBlock(artifact);
    if (staleBlock) li.appendChild(staleBlock);

    const pre = document.createElement("pre");
    pre.className = "artifact-body";
    pre.textContent = artifact.content || "";
    li.appendChild(pre);

    memoryArtifactList.appendChild(li);
  }
  memoryDetails.hidden = memoryArtifacts.length === 0;
  memoryDetails.open = false;

  renderWorkTrace(workTraceEvents);

  overlayList.innerHTML = "";
  if (!overlays.length) {
    overlayList.innerHTML = '<li class="item muted">No corrections or annotations yet.</li>';
  }
  for (const overlay of overlays) {
    const li = document.createElement("li");
    li.className = "item";
    const headDiv = document.createElement("div");
    const headStrong = document.createElement("strong");
    headStrong.textContent = overlay.overlay_type || "overlay";
    headDiv.appendChild(headStrong);
    li.appendChild(headDiv);
    const meta = document.createElement("div");
    meta.className = "muted";
    meta.textContent = formatMetaDateTime(overlay.created_at || "") + " · author: " + (overlay.author || "");
    li.appendChild(meta);
    const pre = document.createElement("pre");
    pre.className = "artifact-body";
    pre.textContent = overlay.content || "";
    li.appendChild(pre);
    overlayList.appendChild(li);
  }
  overlayDetails.open = overlays.length > 0;

  renderLoops(loopArtifacts);
  loopDetails.open = loopArtifacts.length > 0;

  artifactList.innerHTML = "";
  if (!secondaryArtifacts.length) {
    artifactList.innerHTML = '<li class="item muted">No extra support artifacts attached.</li>';
  }
  for (const artifact of secondaryArtifacts) {
    const li = document.createElement("li");
    li.className = "item";
    const headDiv = document.createElement("div");
    const headStrong = document.createElement("strong");
    headStrong.textContent = artifact.artifact_type || "artifact";
    headDiv.appendChild(headStrong);
    li.appendChild(headDiv);
    const meta = document.createElement("div");
    meta.className = "muted";
    meta.textContent = formatMetaDateTime(artifact.created_at || "");
    li.appendChild(meta);
    const prod = document.createElement("div");
    prod.className = "muted";
    prod.textContent = "producer: " + (artifact.producer || "");
    li.appendChild(prod);
    const idDiv = document.createElement("div");
    idDiv.className = "muted";
    idDiv.textContent = "id: " + (artifact.artifact_id || "");
    li.appendChild(idDiv);
    const statusDiv = document.createElement("div");
    statusDiv.className = "muted";
    statusDiv.textContent = "status: " + (artifact.lifecycle_status || "active") + (artifact.is_current ? " · current" : "");
    li.appendChild(statusDiv);
    li.appendChild(createProvenanceBlock(artifact));
    const staleBlock = createOverlayStalenessBlock(artifact);
    if (staleBlock) li.appendChild(staleBlock);
    artifactList.appendChild(li);
  }
  renderArtifactStatusBar(briefArtifacts, loopArtifacts, overlays, memoryArtifacts, workTraceEvents);
  renderInterpHeader(briefArtifacts, loopArtifacts, memoryArtifacts, workTraceEvents);
  artifactDetails.open = false;
}

function summarizePreview(text, size = 140) {
  const compact = String(text || "").replace(/\s+/g, " ").trim();
  if (compact.length <= size) {
    return compact;
  }
  return `${compact.slice(0, size - 3)}...`;
}

function renderSearchResults(matches) {
  searchResults.setAttribute("aria-busy", "false");
  searchResults.innerHTML = "";
  if (!matches.length) {
    searchResults.innerHTML = '<li class="item muted">No results for this search.</li>';
    return;
  }
  for (const hit of matches) {
    const isMemory = hit.match_layer === "compressed_memory";
    const li = document.createElement("li");
    li.className = "item";

    const button = document.createElement("button");
    const entryId = String(hit.entry_id || "");
    button.setAttribute("data-entry-id", entryId);
    button.setAttribute("aria-current", state.selectedSearchHitEntryId === hit.entry_id ? "true" : "false");
    if (state.selectedSearchHitEntryId === hit.entry_id) button.className = "active";

    const badgeWrap = document.createElement("div");
    const layerBadge = document.createElement("span");
    layerBadge.className = isMemory ? "layer-badge layer-badge-memory" : "layer-badge layer-badge-raw";
    layerBadge.textContent = isMemory ? "memory" : "source";
    badgeWrap.appendChild(layerBadge);
    button.appendChild(badgeWrap);

    const meta = document.createElement("div");
    meta.className = "muted";
    meta.textContent = formatMetaDateTime(hit.indexed_at);
    if (hit.artifact_id) {
      meta.append(" · artifact " + String(hit.artifact_id));
    }
    button.appendChild(meta);

    const preview = document.createElement("div");
    preview.className = "preview";
    preview.textContent = hit.match_text || "";
    button.appendChild(preview);

    button.addEventListener("click", async () => {
      state.selectedSearchHitEntryId = hit.entry_id;
      state.openedFromSearchHit = {
        entry_id: hit.entry_id,
        match_text: hit.match_text,
        match_layer: hit.match_layer,
      };
      persistState();
      await loadEntry(hit.entry_id);
    });
    li.appendChild(button);
    searchResults.appendChild(li);
  }
  wireListKeyboardNav(searchResults);
}

function renderImports(items) {
  importsList.setAttribute("aria-busy", "false");
  importsList.innerHTML = "";
  if (!items.length) {
    importsList.innerHTML = '<li class="item muted">No import batches found.</li>';
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    li.className = "item";
    const active = state.importId && state.importId === item.import_id;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.setAttribute("data-import-id", String(item.import_id || ""));
    btn.setAttribute("aria-current", active ? "true" : "false");
    if (active) btn.className = "active";

    const title = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = item.import_id || "unknown import";
    title.appendChild(strong);
    btn.appendChild(title);

    const meta = document.createElement("div");
    meta.className = "muted";
    const metaParts = [formatMetaDateTime(item.imported_at || "")];
    if (item.source_conversation_id) metaParts.push(String(item.source_conversation_id));
    if (item.source_session_id) metaParts.push(String(item.source_session_id));
    meta.textContent = metaParts.join(" · ");
    btn.appendChild(meta);

    const counts = document.createElement("div");
    counts.className = "muted";
    counts.textContent = `imported ${item.imported_count || 0} · skipped duplicates ${item.skipped_duplicate_count || 0}`;
    btn.appendChild(counts);

    btn.addEventListener("click", async () => {
      state.importId = String(item.import_id || "").trim();
      if (item.source_conversation_id) {
        state.sourceConversationId = String(item.source_conversation_id).trim();
      }
      state.truthfulOnly = true;
      state.offset = 0;
      scopeImportIdInput.value = state.importId;
      scopeConversationIdInput.value = state.sourceConversationId;
      scopeTruthfulOnlyInput.checked = state.truthfulOnly;
      persistState();
      writeUrlState();
      renderScopeBar();
      await loadTimeline();
      if (state.searchQuery) {
        await runSearch(state.searchQuery);
      }
    });
    li.appendChild(btn);
    importsList.appendChild(li);
  }
}

function showError(listEl, message) {
  listEl.innerHTML = "";
  listEl.setAttribute("aria-busy", "false");
  const li = document.createElement("li");
  li.className = "item error";
  li.textContent = message;
  listEl.appendChild(li);
}

function wireListKeyboardNav(listEl) {
  const buttons = Array.from(listEl.querySelectorAll("button[data-entry-id]"));
  for (const btn of buttons) {
    btn.addEventListener("keydown", (event) => {
      const currentIndex = buttons.indexOf(btn);
      if (currentIndex === -1) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        const next = buttons[Math.min(buttons.length - 1, currentIndex + 1)];
        next.focus();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        const prev = buttons[Math.max(0, currentIndex - 1)];
        prev.focus();
      } else if (event.key === "Home") {
        event.preventDefault();
        buttons[0].focus();
      } else if (event.key === "End") {
        event.preventDefault();
        buttons[buttons.length - 1].focus();
      }
    });
  }
}

async function loadTimeline() {
  timelineStatus.textContent = "Loading entries...";
  timelineList.setAttribute("aria-busy", "true");
  try {
    const filters = {};
    if (state.sourceConversationId) filters.source_conversation_id = state.sourceConversationId;
    if (state.sourceSessionId) filters.source_session_id = state.sourceSessionId;
    if (state.importId) filters.import_id = state.importId;
    if (state.truthfulOnly) filters.truthful_only = true;
    const result = await post("/list_entries", {
      limit: state.limit,
      offset: state.offset,
      filters,
    });
    renderTimeline(result.items || []);
    const scopeParts = [];
    if (state.sourceConversationId) scopeParts.push(`conversation=${state.sourceConversationId}`);
    if (state.importId) scopeParts.push(`import=${state.importId}`);
    if (state.truthfulOnly) scopeParts.push("truthful-only");
    const scopeLabel = scopeParts.length ? ` · scope ${scopeParts.join(", ")}` : "";
    timelineStatus.textContent = `Showing ${result.items.length} of ${result.total ?? "?"} entries${scopeLabel}`;
  } catch (err) {
    showError(timelineList, `Timeline error: ${err.message}`);
    timelineStatus.textContent = "Could not load entries.";
  }
}

async function loadImports() {
  importsStatus.textContent = "Loading...";
  importsList.setAttribute("aria-busy", "true");
  try {
    const result = await post("/list_imports", { limit: 20 });
    renderImports(result.items || []);
    importsStatus.textContent = `${result.count || 0} recent`;
  } catch (err) {
    showError(importsList, `Import batches error: ${err.message}`);
    importsStatus.textContent = "Load failed";
  }
}

async function loadEntry(entryId) {
  detailStatus.textContent = "Loading entry...";
  try {
    const detail = await post("/fetch_entry_detail", { entry_id: entryId });
    state.selectedEntryId = entryId;
    selectedTimelineContext = {
      entry_id: detail.raw_entry.entry_id,
      created_at: detail.raw_entry.created_at,
      entry_type: detail.raw_entry.entry_type,
      source: detail.raw_entry.source,
      author_role: detail.raw_entry.author_role,
      preview: summarizePreview(detail.raw_entry.content),
    };
    persistState();
    writeUrlState();
    renderDetail(detail);
    if (state.openedFromSearchHit?.entry_id === entryId) {
      renderRecallBanner(state.openedFromSearchHit);
    } else {
      clearRecallBanner();
    }
    await loadTimeline();
  } catch (err) {
    detailBody.textContent = `Entry load error: ${err.message}`;
    detailStatus.textContent = "Could not load entry.";
  }
}

async function submitOverlay() {
  if (!state.selectedEntryId) {
    overlayStatus.textContent = "Select an entry first.";
    return;
  }
  const overlayType = overlayTypeInput.value.trim();
  const author = overlayAuthorInput.value.trim();
  const content = overlayContentInput.value.trim();
  if (!overlayType || !author || !content) {
    overlayStatus.textContent = "Type, author, and note are required.";
    return;
  }
  overlayStatus.textContent = "Saving overlay...";
  try {
    await post("/append_overlay", {
      entry_id: state.selectedEntryId,
      overlay_type: overlayType,
      author,
      content,
    });
    overlayContentInput.value = "";
    overlayStatus.textContent = "Note saved.";
    await loadEntry(state.selectedEntryId);
  } catch (err) {
    overlayStatus.textContent = `Note save failed: ${err.message}`;
  }
}

function producerPayloadFromCurrentContext() {
  const payload = { limit: 200, force: true };
  const hasScope = Boolean(state.sourceConversationId || state.sourceSessionId || state.importId || state.truthfulOnly);
  if (hasScope) {
    if (state.sourceConversationId) payload.source_conversation_id = state.sourceConversationId;
    if (state.sourceSessionId) payload.source_session_id = state.sourceSessionId;
    if (state.importId) payload.import_id = state.importId;
    if (state.truthfulOnly) payload.truthful_only = true;
    return payload;
  }
  if (state.selectedEntryId) {
    payload.entry_ids = [state.selectedEntryId];
  }
  return payload;
}

function setRefreshDerivedButtonsDisabled(disabled) {
  for (const btn of [refreshOpenLoopsBtn, refreshBriefsBtn, refreshMemoryBtn]) {
    btn.disabled = disabled;
  }
}

function summarizeProducerResult(endpoint, result) {
  if (endpoint === "/produce_open_loops") {
    return `Follow-ups refreshed. loops=${result.loop_count || 0} source_entries=${(result.source_entry_ids || []).length}`;
  }
  if (endpoint === "/produce_conversation_briefs") {
    return `Summaries refreshed. produced=${result.produced_count || 0} skipped=${result.skipped_count || 0}`;
  }
  if (endpoint === "/produce_compressed_memory") {
    return `Search memory refreshed. produced=${result.produced_count || 0} skipped=${result.skipped_count || 0}`;
  }
  return "Generated helper refreshed.";
}

async function refreshDerived(endpoint) {
  const payload = producerPayloadFromCurrentContext();
  refreshDerivedScope.textContent = describeRefreshScope(payload);
  if (!payload.entry_ids && !payload.source_conversation_id && !payload.source_session_id && !payload.import_id && !payload.truthful_only) {
    refreshDerivedStatus.textContent = "Select an entry or apply an advanced filter first.";
    return;
  }
  const label =
    endpoint === "/produce_open_loops"
      ? "open loops"
      : endpoint === "/produce_conversation_briefs"
        ? "conversation briefs"
        : "search memory";
  refreshDerivedStatus.textContent = `Refreshing ${label}...`;
  setRefreshDerivedButtonsDisabled(true);
  try {
    const result = await post(endpoint, payload);
    refreshDerivedStatus.textContent = summarizeProducerResult(endpoint, result);
    await loadTimeline();
    if (state.searchQuery) {
      await runSearch(state.searchQuery);
    }
    if (state.selectedEntryId) {
      await loadEntry(state.selectedEntryId);
    }
  } catch (err) {
    refreshDerivedStatus.textContent = `Refresh failed: ${err.message}`;
  } finally {
    setRefreshDerivedButtonsDisabled(false);
  }
}

async function runSearch(query) {
  searchStatus.textContent = "Searching diary...";
  searchResults.setAttribute("aria-busy", "true");
  try {
    const filters = {};
    if (state.sourceConversationId) filters.source_conversation_id = state.sourceConversationId;
    if (state.sourceSessionId) filters.source_session_id = state.sourceSessionId;
    if (state.importId) filters.import_id = state.importId;
    if (state.truthfulOnly) filters.truthful_only = true;
    const result = await post("/search_memory", { query, limit: 20, filters });
    state.searchQuery = query;
    persistState();
    writeUrlState();
    renderSearchResults(result.matches || []);
    const summary = result.match_summary || {};
    if (summary.using_fallback) {
      searchStatus.textContent = `Found ${(result.matches || []).length} source source-record hits.`;
    } else {
      searchStatus.textContent = `Found ${(result.matches || []).length} generated memory hits.`;
    }
  } catch (err) {
    showError(searchResults, `Search error: ${err.message}`);
    searchStatus.textContent = "Search failed.";
  }
}

function persistState() {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      apiBase: state.apiBase,
      limit: state.limit,
      offset: state.offset,
      sourceConversationId: state.sourceConversationId,
      sourceSessionId: state.sourceSessionId,
      importId: state.importId,
      truthfulOnly: state.truthfulOnly,
      selectedEntryId: state.selectedEntryId,
      searchQuery: state.searchQuery,
      selectedSearchHitEntryId: state.selectedSearchHitEntryId,
    })
  );
}

function writeUrlState() {
  if (isApplyingUrlState) return;
  const params = new URLSearchParams(window.location.search);
  if (state.selectedEntryId) {
    params.set("entry", state.selectedEntryId);
  } else {
    params.delete("entry");
  }
  if (state.searchQuery) {
    params.set("q", state.searchQuery);
  } else {
    params.delete("q");
  }
  if (state.offset > 0) {
    params.set("offset", String(state.offset));
  } else {
    params.delete("offset");
  }
  if (state.sourceConversationId) {
    params.set("source_conversation_id", state.sourceConversationId);
  } else {
    params.delete("source_conversation_id");
  }
  if (state.importId) {
    params.set("import_id", state.importId);
  } else {
    params.delete("import_id");
  }
  if (state.truthfulOnly) {
    params.set("truthful_only", "1");
  } else {
    params.delete("truthful_only");
  }
  const next = `${window.location.pathname}${params.toString() ? `?${params.toString()}` : ""}`;
  window.history.replaceState({}, "", next);
}

function restoreStateFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const entry = params.get("entry");
  const q = params.get("q");
  const offset = params.get("offset");
  const sourceConversationId = params.get("source_conversation_id");
  const importId = params.get("import_id");
  const truthfulOnly = params.get("truthful_only");
  let found = false;
  if (entry) {
    state.selectedEntryId = entry;
    found = true;
  }
  if (q) {
    state.searchQuery = q;
    found = true;
  }
  if (offset !== null) {
    const parsed = Number.parseInt(offset, 10);
    if (Number.isFinite(parsed) && parsed >= 0) {
      state.offset = parsed;
      found = true;
    }
  }
  if (sourceConversationId !== null) {
    state.sourceConversationId = sourceConversationId.trim();
    found = true;
  }
  if (importId !== null) {
    state.importId = importId.trim();
    found = true;
  }
  if (truthfulOnly !== null) {
    state.truthfulOnly = truthfulOnly === "1" || truthfulOnly.toLowerCase() === "true";
    found = true;
  }
  return found;
}

function restoreState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    state.apiBase = parsed.apiBase || state.apiBase;
    state.limit = Number.isInteger(parsed.limit) ? parsed.limit : state.limit;
    state.offset = Number.isInteger(parsed.offset) ? parsed.offset : state.offset;
    state.sourceConversationId = parsed.sourceConversationId || "";
    state.sourceSessionId = parsed.sourceSessionId || "";
    state.importId = parsed.importId || "";
    state.truthfulOnly = Boolean(parsed.truthfulOnly);
    state.selectedEntryId = parsed.selectedEntryId || null;
    state.searchQuery = parsed.searchQuery || "";
    state.selectedSearchHitEntryId = parsed.selectedSearchHitEntryId || null;
  } catch (_err) {
    // Ignore malformed local state and continue with defaults.
  }
}

reloadBtn.addEventListener("click", async () => {
  state.apiBase = apiBaseInput.value.trim().replace(/\/$/, "");
  state.offset = 0;
  persistState();
  writeUrlState();
  renderScopeBar();
  await loadTimeline();
  await loadImports();
  checkConnection();
});

prevPageBtn.addEventListener("click", async () => {
  state.offset = Math.max(0, state.offset - state.limit);
  persistState();
  writeUrlState();
  await loadTimeline();
});

nextPageBtn.addEventListener("click", async () => {
  state.offset += state.limit;
  persistState();
  writeUrlState();
  await loadTimeline();
});

refreshImportsBtn.addEventListener("click", async () => {
  await loadImports();
});

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = searchInput.value.trim();
  if (!query) return;
  await runSearch(query);
});

overlayForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await submitOverlay();
});

refreshOpenLoopsBtn.addEventListener("click", async () => {
  await refreshDerived("/produce_open_loops");
});

refreshBriefsBtn.addEventListener("click", async () => {
  await refreshDerived("/produce_conversation_briefs");
});

refreshMemoryBtn.addEventListener("click", async () => {
  await refreshDerived("/produce_compressed_memory");
});

browseScopeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  state.sourceConversationId = scopeConversationIdInput.value.trim();
  state.importId = scopeImportIdInput.value.trim();
  state.truthfulOnly = Boolean(scopeTruthfulOnlyInput.checked);
  state.offset = 0;
  persistState();
  writeUrlState();
  renderScopeBar();
  await loadTimeline();
  await loadImports();
});

clearScopeBtn.addEventListener("click", async () => {
  await clearScopeState();
});

async function init() {
  const hasUrlState = restoreStateFromUrl();
  if (!hasUrlState) {
    restoreState();
  }
  apiBaseInput.value = state.apiBase;
  searchInput.value = state.searchQuery;
  scopeConversationIdInput.value = state.sourceConversationId;
  scopeImportIdInput.value = state.importId;
  scopeTruthfulOnlyInput.checked = state.truthfulOnly;
  state.apiBase = apiBaseInput.value.trim().replace(/\/$/, "");
  renderScopeBar();
  await loadTimeline();
  checkConnection();
  if (state.searchQuery) {
    await runSearch(state.searchQuery);
  } else {
    searchStatus.textContent = "Search looks across generated memory first, then falls back to raw source entries.";
  }
  if (state.selectedEntryId) {
    await loadEntry(state.selectedEntryId);
  } else {
    detailMeta.innerHTML = "";
    detailStatus.textContent = "Search or choose a recent entry.";
    interpHeader.textContent = "";
    artifactStatusBar.innerHTML = "";
    loopList.innerHTML = "";
    refreshDerivedScope.textContent = "";
    artifactDetails.open = false;
    loopDetails.open = false;
    overlayDetails.open = false;
    refreshDerivedDetails.open = false;
    memoryDetails.hidden = true;
    memoryDetails.open = false;
    overlayStatus.textContent = "";
    refreshDerivedStatus.textContent = "";
    clearRecallBanner();
    clearDetailBody();
    detailBody.textContent = "Search or choose a recent entry to read the source record.";
  }
  writeUrlState();
}

window.addEventListener("popstate", async () => {
  isApplyingUrlState = true;
  clearRecallBanner();
  const snapshot = {
    selectedEntryId: state.selectedEntryId,
    searchQuery: state.searchQuery,
    offset: state.offset,
    sourceConversationId: state.sourceConversationId,
    sourceSessionId: state.sourceSessionId,
    importId: state.importId,
    truthfulOnly: state.truthfulOnly,
  };
  state.selectedEntryId = null;
  state.searchQuery = "";
  state.offset = 0;
  state.sourceConversationId = "";
  state.sourceSessionId = "";
  state.importId = "";
  state.truthfulOnly = false;
  const hasUrlState = restoreStateFromUrl();
  if (!hasUrlState) {
    state.selectedEntryId = snapshot.selectedEntryId;
    state.searchQuery = snapshot.searchQuery;
    state.offset = snapshot.offset;
    state.sourceConversationId = snapshot.sourceConversationId;
    state.sourceSessionId = snapshot.sourceSessionId;
    state.importId = snapshot.importId;
    state.truthfulOnly = snapshot.truthfulOnly;
  }
  searchInput.value = state.searchQuery;
  scopeConversationIdInput.value = state.sourceConversationId;
  scopeImportIdInput.value = state.importId;
  scopeTruthfulOnlyInput.checked = state.truthfulOnly;
  renderScopeBar();
  await loadTimeline();
  if (state.searchQuery) {
    await runSearch(state.searchQuery);
  } else {
    searchResults.innerHTML = "";
    searchStatus.textContent = "Search looks across generated memory first, then falls back to raw source entries.";
  }
  if (state.selectedEntryId) {
    await loadEntry(state.selectedEntryId);
  } else {
    detailMeta.innerHTML = "";
    clearDetailBody();
    detailBody.textContent = "Search or choose a recent entry to read the source record.";
    detailStatus.textContent = "Search or choose a recent entry.";
    interpHeader.textContent = "";
    artifactStatusBar.innerHTML = "";
    loopList.innerHTML = "";
    refreshDerivedScope.textContent = "";
    memoryDetails.hidden = true;
    memoryDetails.open = false;
    overlayStatus.textContent = "";
    refreshDerivedStatus.textContent = "";
    overlayList.innerHTML = "";
    overlayDetails.open = false;
    memoryArtifactList.innerHTML = "";
    artifactList.innerHTML = "";
    loopDetails.open = false;
  }
  isApplyingUrlState = false;
});

// Tab switching for right panel
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.remove("active");
      b.setAttribute("aria-selected", "false");
    });
    btn.classList.add("active");
    btn.setAttribute("aria-selected", "true");
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    const panel = document.getElementById(`tab-${btn.dataset.tab}`);
    if (panel) panel.classList.add("active");
  });
});

init();
