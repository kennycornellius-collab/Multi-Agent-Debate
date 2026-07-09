(() => {
  "use strict";

  const STORAGE_KEY = "debate-pipeline-run";
  const BUILD_ORDER = ["Architect", "Coder", "Reviewer", "Tester"];
  const DEBATE_ORDER = ["Strategist", "Critic", "Refiner"];
  const FILE_PRIORITY = ["agreed_spec.md", "architecture.md", "review.md", "tests.md"];
  const SCROLL_BOTTOM_THRESHOLD = 40;
  const MODEL_CHECK_DEBOUNCE_MS = 400;

  const ideaRow = document.getElementById("idea-row");
  const specRow = document.getElementById("spec-row");
  const roundsRow = document.getElementById("rounds-row");
  const targetPathRow = document.getElementById("target-path-row");
  const ideaInput = document.getElementById("idea");
  const specTextInput = document.getElementById("spec-text");
  const targetPathInput = document.getElementById("target-path");
  const roundsInput = document.getElementById("rounds");
  const debateFeedTitle = document.getElementById("debate-feed-title");
  const debateFeedMetaEl = document.getElementById("debate-feed-meta");
  const buildFeedMetaEl = document.getElementById("build-feed-meta");
  const runBtn = document.getElementById("run-btn");
  const cancelBtn = document.getElementById("cancel-btn");
  const runErrorEl = document.getElementById("run-error");
  const pausedBannerEl = document.getElementById("paused-banner");
  const modelStatusEl = document.getElementById("model-status");
  const debateFeedSection = document.getElementById("debate-feed");
  const buildFeedSection = document.getElementById("build-feed");
  const debateFeedBody = document.querySelector("#debate-feed .feed-body");
  const buildFeedBody = document.querySelector("#build-feed .feed-body");
  const fileListEl = document.getElementById("file-list");
  const fileViewEl = document.getElementById("file-view");
  const fileCountEl = document.getElementById("file-count");
  const runDotEl = document.getElementById("run-dot");
  const runIdChipEl = document.getElementById("run-id-chip");
  const runPhaseLabelEl = document.getElementById("run-phase-label");
  const costTagEl = document.getElementById("cost-tag");
  const configToggleEl = document.getElementById("config-toggle");
  const configPanelEl = document.getElementById("config-panel");
  const chipModeEl = document.getElementById("chip-mode");
  const chipRoundsEl = document.getElementById("chip-rounds");
  const chipModelEl = document.getElementById("chip-model");
  const chipEffortEl = document.getElementById("chip-effort");
  const trackerListEl = document.getElementById("tracker-list");
  const roundsSepEl = document.getElementById("rounds-sep");

  const state = {
    runId: null,
    numRounds: 3,
    mode: "full",
    eventSource: null,
    cards: new Map(), // key -> { el, head, status, body }
    activeCard: null, // the one card currently marked .active/.bracket, if any
    selectedFile: null,
    totalCost: 0,
    modelCheckSeq: 0, // guards against a stale check response overwriting a newer one
  };

  function clamp(n, lo, hi) {
    return Math.max(lo, Math.min(hi, n));
  }

  function capitalize(s) {
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  // config.py's AVAILABLE_MODELS/AVAILABLE_EFFORT_LEVELS are plain lowercase CLI values;
  // "xhigh" is the one irregular case that a naive capitalize() can't produce correctly.
  function formatValueLabel(value) {
    if (value === "xhigh") return "X-High";
    return capitalize(value);
  }

  // ---------- Pill groups (Mode / Model / Effort) ----------

  function getPillGroup(name) {
    return document.querySelector(`.pill-group[data-group="${name}"]`);
  }

  function getSelectedPill(name) {
    const group = getPillGroup(name);
    return group ? group.querySelector(".pill.selected") : null;
  }

  function getSelectedPillValue(name) {
    const pill = getSelectedPill(name);
    return pill ? pill.dataset.value : "";
  }

  function getSelectedPillLabel(name) {
    const pill = getSelectedPill(name);
    return pill ? pill.textContent : "";
  }

  function selectPill(name, value) {
    const group = getPillGroup(name);
    if (!group) return;
    for (const p of group.querySelectorAll(".pill")) {
      p.classList.toggle("selected", p.dataset.value === value);
    }
  }

  function makePill(value, label, selected) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `pill${selected ? " selected" : ""}`;
    btn.dataset.value = value;
    btn.textContent = label;
    return btn;
  }

  // Rebuilds a pill-group's options from a fetched config list, always keeping "Default"
  // (value "") first. Preserves whatever was selected before the rebuild when possible --
  // the hardcoded fallback pills already in index.html match config.py's current values,
  // so in the common case this is a no-op refresh, not a visible change.
  function rebuildValuePills(name, values) {
    const group = getPillGroup(name);
    if (!group) return;
    const previous = getSelectedPillValue(name);
    group.replaceChildren(makePill("", "Default", previous === ""));
    for (const value of values) {
      group.appendChild(makePill(value, formatValueLabel(value), value === previous));
    }
  }

  function wirePillGroup(name, onChange) {
    const group = getPillGroup(name);
    if (!group) return;
    group.addEventListener("click", (e) => {
      const btn = e.target.closest(".pill");
      if (!btn) return;
      selectPill(name, btn.dataset.value);
      onChange(btn.dataset.value);
    });
  }

  // ---------- Config strip (collapse/expand + summary chips) ----------

  function setConfigCollapsed(collapsed) {
    configToggleEl.setAttribute("aria-expanded", String(!collapsed));
    configPanelEl.hidden = collapsed;
  }

  function updateConfigSummary() {
    const rounds = roundsInput.value || "3";
    chipModeEl.textContent = `Mode: ${getSelectedPillLabel("mode") || "Full"}`;
    chipRoundsEl.textContent = `Rounds: ${rounds}`;
    chipModelEl.textContent = `Model: ${getSelectedPillLabel("model") || "Default"}`;
    chipEffortEl.textContent = `Effort: ${getSelectedPillLabel("effort") || "Default"}`;
    roundsSepEl.textContent = `×${rounds} Rounds`;
  }

  // ---------- Mode-driven visibility (form fields, feeds, tracker steps) ----------

  function setStepHidden(key, hidden) {
    const li = trackerListEl.querySelector(`[data-step="${CSS.escape(key)}"]`);
    if (li) li.hidden = hidden;
  }

  function applyModeVisibility(mode) {
    const isBuildOnly = mode === "build_only";
    const isDebateOnly = mode === "debate_only";
    const isCodebase = mode === "codebase";
    ideaRow.hidden = isBuildOnly;
    specRow.hidden = !isBuildOnly;
    roundsRow.hidden = isBuildOnly;
    targetPathRow.hidden = !isCodebase;
    debateFeedSection.hidden = isBuildOnly;
    buildFeedSection.hidden = isDebateOnly;
    ideaInput.placeholder = isCodebase
      ? "Describe the bug or feature to investigate, e.g. add(2, 3) returns -1 instead of 5"
      : "Describe a project idea, e.g. a CLI tool that renames photos by EXIF date";
    // Sandbox/recon events (codebase mode's pre-debate steps) land in this panel too --
    // see feedBodyFor() -- so its heading should reflect that instead of implying only
    // debate cards ever show up there.
    debateFeedTitle.textContent = isCodebase ? "Investigation & Debate" : "Debate";

    setStepHidden("Sandbox", !isCodebase);
    setStepHidden("Recon", !isCodebase);
    for (const key of DEBATE_ORDER) setStepHidden(key, isBuildOnly);
    roundsSepEl.hidden = isBuildOnly;
    for (const key of BUILD_ORDER) setStepHidden(key, isDebateOnly);
  }

  function showError(msg) {
    runErrorEl.textContent = msg;
    runErrorEl.hidden = false;
  }

  function hideError() {
    runErrorEl.hidden = true;
    runErrorEl.textContent = "";
  }

  // Usage-Limit Resilience addon: a distinct banner from showError/hideError above --
  // a paused run isn't an error, it's expected to resume on its own (or via Cancel).
  function showPaused(retryAt) {
    pausedBannerEl.textContent = retryAt
      ? `Paused — usage limit reached, resumes ~${new Date(retryAt).toLocaleString()}`
      : "Paused — usage limit reached, waiting to retry…";
    pausedBannerEl.hidden = false;
  }

  function hidePaused() {
    pausedBannerEl.hidden = true;
    pausedBannerEl.textContent = "";
  }

  function runDoneLabel(content) {
    if (!content) return "Done";
    return /cancelled/i.test(content) ? "Cancelled" : "Finished with errors";
  }

  function persistRun(runId, numRounds, mode) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ runId, numRounds, mode }));
  }

  function clearPersistedRun() {
    localStorage.removeItem(STORAGE_KEY);
  }

  async function loadUiConfig() {
    try {
      const res = await fetch("/config");
      if (!res.ok) return;
      const data = await res.json();
      rebuildValuePills("model", data.models || []);
      rebuildValuePills("effort", data.effort_levels || []);
    } catch {
      // /config unreachable: the hardcoded fallback pills already in index.html stay as-is.
    }
    updateConfigSummary();
  }

  function setModelStatus(text, cls) {
    modelStatusEl.textContent = text;
    modelStatusEl.className = `model-status${cls ? ` ${cls}` : ""}`;
  }

  let modelCheckTimer = null;

  function scheduleModelCheck() {
    const model = getSelectedPillValue("model");
    clearTimeout(modelCheckTimer);
    if (!model) {
      setModelStatus("", "");
      return;
    }
    setModelStatus("checking…", "checking");
    modelCheckTimer = setTimeout(() => checkModel(model), MODEL_CHECK_DEBOUNCE_MS);
  }

  async function checkModel(model) {
    const seq = ++state.modelCheckSeq;
    try {
      const res = await fetch("/models/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model }),
      });
      if (seq !== state.modelCheckSeq || getSelectedPillValue("model") !== model) return; // stale
      if (!res.ok) {
        setModelStatus((await safeDetail(res)) || `check failed (${res.status})`, "unavailable");
        return;
      }
      const data = await res.json();
      if (data.available) {
        setModelStatus("✓ available", "available");
      } else {
        setModelStatus(`✗ ${data.message || "not available"}`, "unavailable");
      }
    } catch (err) {
      if (seq !== state.modelCheckSeq || getSelectedPillValue("model") !== model) return;
      setModelStatus(`network error: ${err.message}`, "unavailable");
    }
  }

  function feedBodyFor(phase) {
    // "build" is the only phase that belongs in the build feed; everything else --
    // "debate", plus codebase mode's pre-debate "sandbox"/"recon" phases -- reads
    // naturally as part of "getting to and having the debate", so it shares that panel.
    return phase === "build" ? buildFeedBody : debateFeedBody;
  }

  function cardKey(ev) {
    return `${ev.phase}|${ev.round ?? ""}|${ev.agent}`;
  }

  function withAutoScroll(container, fn) {
    const atBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight <= SCROLL_BOTTOM_THRESHOLD;
    fn();
    if (atBottom) container.scrollTop = container.scrollHeight;
  }

  // Marks `card` as the one actively-streaming card (corner-bracket highlight). At most
  // one card is ever truly live at a time (the pipeline runs one agent turn at a time),
  // so promoting a new one always demotes whichever was previously active.
  function setActiveCard(card) {
    if (state.activeCard && state.activeCard !== card) {
      state.activeCard.el.classList.remove("active", "bracket");
    }
    state.activeCard = card || null;
    if (card) card.el.classList.add("active", "bracket");
  }

  // dot: "active" (amber, pulsing) while streaming, "success" (green) when done, or
  // omitted entirely for error/paused/queued -- mirrors the mockup's own convention of
  // only pairing a status dot with Streaming/Done, plain text otherwise.
  function setCardStatus(card, text, dotKind) {
    card.status.replaceChildren();
    if (dotKind) {
      const dot = document.createElement("span");
      dot.className = dotKind === "active" ? "dot active pulse" : `dot ${dotKind}`;
      card.status.appendChild(dot);
    }
    card.status.appendChild(document.createTextNode(text));
  }

  function getOrCreateCard(ev) {
    const key = cardKey(ev);
    let card = state.cards.get(key);
    if (card) return card;

    const isSynthesis = ev.phase === "debate" && ev.agent === "Refiner" && ev.round == null;
    const el = document.createElement("div");
    el.className = `card agent-${(ev.agent || "system").toLowerCase()}${isSynthesis ? " synthesis" : ""}`;

    const head = document.createElement("div");
    head.className = "card-head";

    const led = document.createElement("span");
    led.className = "led";

    const tag = document.createElement("span");
    tag.className = "agent-tag";
    tag.textContent = ev.agent || "system";

    head.appendChild(led);
    head.appendChild(tag);

    if (ev.phase === "debate") {
      const roundPill = document.createElement("span");
      roundPill.className = "round-pill";
      roundPill.textContent = ev.round == null ? "Final Synthesis" : `R${ev.round}`;
      head.appendChild(roundPill);
    }

    const status = document.createElement("span");
    status.className = "card-status";
    head.appendChild(status);

    const body = document.createElement("div");
    body.className = "card-body";

    el.appendChild(head);
    el.appendChild(body);

    const container = feedBodyFor(ev.phase);
    withAutoScroll(container, () => container.appendChild(el));

    card = { el, head, status, body };
    state.cards.set(key, card);
    setCardStatus(card, "Streaming", "active");
    setActiveCard(card);
    return card;
  }

  // ---------- Tracker + topbar + feed-meta (the step-state/cost/phase-label HUD) ----------

  function stepKeyFor(ev) {
    if (ev.phase === "sandbox") return "Sandbox";
    if (ev.phase === "recon") return "Recon";
    if (ev.phase === "debate" || ev.phase === "build") return ev.agent;
    return null;
  }

  function setStepState(key, cls) {
    if (!key) return;
    const li = trackerListEl.querySelector(`[data-step="${CSS.escape(key)}"]`);
    if (!li) return;
    li.classList.remove("done", "active");
    if (cls) li.classList.add(cls);
  }

  function resetTracker() {
    for (const li of trackerListEl.querySelectorAll(".step")) {
      li.classList.remove("done", "active");
    }
  }

  function updateCost(ev) {
    if (ev.cost_usd == null) return;
    state.totalCost = ev.cost_usd;
    costTagEl.textContent = `$${state.totalCost.toFixed(4)}`;
  }

  function updateHud(ev) {
    updateCost(ev);
    if (ev.type === "agent_start") {
      setStepState(stepKeyFor(ev), "active");
      runDotEl.className = "dot active pulse";
      if (ev.phase === "sandbox") {
        runPhaseLabelEl.textContent = "Preparing sandbox…";
      } else if (ev.phase === "recon") {
        runPhaseLabelEl.textContent = "Investigating codebase (Recon)…";
        debateFeedMetaEl.textContent = "Recon";
      } else if (ev.phase === "debate") {
        const n = state.numRounds || 3;
        const roundLabel = ev.round == null ? "Final synthesis" : `Round ${ev.round} of ${n}`;
        runPhaseLabelEl.textContent = `Debate · ${roundLabel} · ${ev.agent}`;
        debateFeedMetaEl.textContent = roundLabel;
      } else if (ev.phase === "build") {
        const idx = BUILD_ORDER.indexOf(ev.agent);
        buildFeedMetaEl.textContent = idx === -1 ? ev.agent : `Step ${idx + 1} of ${BUILD_ORDER.length} · ${ev.agent}`;
        runPhaseLabelEl.textContent = `Build · ${ev.agent}`;
      }
    } else if (ev.type === "agent_done" || ev.type === "error") {
      setStepState(stepKeyFor(ev), "done");
    } else if (ev.type === "phase_done") {
      if (ev.phase === "sandbox") {
        setStepState("Sandbox", "done");
      } else if (ev.phase === "recon") {
        setStepState("Recon", "done");
      } else if (ev.phase === "debate") {
        for (const key of DEBATE_ORDER) setStepState(key, "done");
        debateFeedMetaEl.textContent = "Done";
      } else if (ev.phase === "build") {
        for (const key of BUILD_ORDER) setStepState(key, "done");
        buildFeedMetaEl.textContent = "Done";
      }
    } else if (ev.type === "run_done") {
      runPhaseLabelEl.textContent = runDoneLabel(ev.content);
      runDotEl.className = ev.content ? "dot error" : "dot success";
    }
  }

  function handleEvent(ev) {
    updateHud(ev);
    switch (ev.type) {
      case "agent_start": {
        const key = cardKey(ev);
        const isRetry = state.cards.has(key);
        const card = getOrCreateCard(ev);
        if (isRetry) {
          // A second agent_start for the same key means the previous attempt failed
          // after streaming some partial text (retry-once logic in debate.py/build.py) --
          // clear it so the retry's own output doesn't get concatenated onto the stale
          // fragment left behind by the failed attempt.
          card.body.textContent = "";
          card.body.classList.remove("dim");
          card.el.classList.remove("error", "compact");
          setCardStatus(card, "Streaming", "active");
          setActiveCard(card);
        }
        break;
      }
      case "delta": {
        const card = getOrCreateCard(ev);
        const container = feedBodyFor(ev.phase);
        withAutoScroll(container, () => {
          card.body.textContent += ev.content;
        });
        break;
      }
      case "agent_done": {
        const card = getOrCreateCard(ev);
        card.el.classList.add("compact");
        card.body.classList.add("dim");
        setCardStatus(card, ev.content ? `Done (${ev.content})` : "Done", "success");
        if (state.activeCard === card) setActiveCard(null);
        break;
      }
      case "error": {
        const card = getOrCreateCard(ev);
        card.el.classList.add("error", "compact");
        card.body.classList.add("dim");
        setCardStatus(card, "Error");
        if (ev.content) card.body.textContent += `\n[error] ${ev.content}`;
        if (state.activeCard === card) setActiveCard(null);
        break;
      }
      case "paused": {
        // Usage-Limit Resilience addon: run_agent_streaming is waiting out a usage-limit
        // exhaustion and will retry the identical call once it's over -- not a failure,
        // so no ".error" class here, just a distinct waiting state + the global banner.
        const card = getOrCreateCard(ev);
        card.el.classList.add("paused", "compact");
        card.body.classList.add("dim");
        setCardStatus(
          card,
          ev.retry_at ? `Paused · resumes ~${new Date(ev.retry_at).toLocaleString()}` : "Paused · waiting"
        );
        if (state.activeCard === card) setActiveCard(null);
        showPaused(ev.retry_at);
        break;
      }
      case "resumed": {
        // The upcoming retry re-runs the identical call from scratch, so it will
        // re-stream the whole reply -- clear the stale pre-pause fragment first so it
        // doesn't get concatenated onto the fresh output (same rationale as the
        // agent_start retry-clear branch above).
        const card = getOrCreateCard(ev);
        card.body.textContent = "";
        card.body.classList.remove("dim");
        card.el.classList.remove("paused", "compact");
        setCardStatus(card, "Streaming", "active");
        setActiveCard(card);
        hidePaused();
        break;
      }
      case "files_updated":
        refreshFiles();
        break;
      case "phase_done":
        refreshFiles();
        break;
      case "run_done":
        finishRun(ev.content);
        break;
      default:
        break; // unknown/future event types: ignore, never crash the stream
    }
  }

  function connect(runId) {
    state.runId = runId;
    if (state.eventSource) state.eventSource.close();
    const es = new EventSource(`/stream/${runId}`);
    es.onmessage = (e) => {
      let ev;
      try {
        ev = JSON.parse(e.data);
      } catch {
        return;
      }
      handleEvent(ev);
    };
    state.eventSource = es;
  }

  function finishRun(content) {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    runBtn.disabled = false;
    cancelBtn.hidden = true;
    setConfigCollapsed(false);
    hidePaused();
    clearPersistedRun();
    if (content) showError(content);
    refreshFiles();
  }

  async function cancelRun() {
    if (!state.runId) return;
    cancelBtn.disabled = true;
    try {
      await fetch(`/run/${state.runId}/cancel`, { method: "POST" });
      // No local state change here -- the real _run_pipeline cancellation lands as a
      // run_done event over the existing SSE stream (finishRun handles it the same as
      // any other terminal state), so there's nothing to do but wait for it.
    } catch {
      // network hiccup; the user can just click Cancel again
    } finally {
      cancelBtn.disabled = false;
    }
  }

  function resetFeeds() {
    debateFeedBody.replaceChildren();
    buildFeedBody.replaceChildren();
    state.cards.clear();
    state.activeCard = null;
    fileListEl.replaceChildren();
    fileViewEl.textContent = "Select a file to view its contents.";
    fileCountEl.textContent = "0 Files";
    state.selectedFile = null;
    state.totalCost = 0;
    costTagEl.textContent = "$0.00";
    resetTracker();
    debateFeedMetaEl.textContent = "Idle";
    buildFeedMetaEl.textContent = "Idle";
  }

  async function safeDetail(res) {
    try {
      const data = await res.json();
      return data && data.detail;
    } catch {
      return null;
    }
  }

  async function startRun() {
    hideError();
    const mode = getSelectedPillValue("mode");
    let idea = "";
    let specText = "";
    let targetPath = "";
    if (mode === "build_only") {
      specText = specTextInput.value.trim();
      if (!specText) {
        showError("Please paste a spec for build-only mode.");
        return;
      }
    } else {
      idea = ideaInput.value.trim();
      if (!idea) {
        showError(mode === "codebase" ? "Please describe the bug or feature to investigate." : "Please enter a project idea.");
        return;
      }
      if (mode === "codebase") {
        targetPath = targetPathInput.value.trim();
        if (!targetPath) {
          showError("Please enter the path to the existing codebase.");
          return;
        }
      }
    }
    const numRounds = clamp(parseInt(roundsInput.value, 10) || 3, 1, 8);
    const model = getSelectedPillValue("model") || null;
    const effort = getSelectedPillValue("effort") || null;

    runBtn.disabled = true;
    try {
      const res = await fetch("/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          idea,
          spec_text: specText,
          target_path: targetPath,
          num_rounds: numRounds,
          agents: [],
          model,
          effort,
          mode,
        }),
      });
      if (!res.ok) {
        showError((await safeDetail(res)) || `Request failed (${res.status})`);
        runBtn.disabled = false;
        return;
      }
      const data = await res.json();
      state.mode = mode;
      applyModeVisibility(mode);
      resetFeeds();
      hidePaused();
      setConfigCollapsed(true);
      cancelBtn.hidden = false;
      state.numRounds = numRounds;
      runIdChipEl.textContent = data.run_id;
      runPhaseLabelEl.textContent = "Starting…";
      runDotEl.className = "dot active pulse";
      if (mode === "codebase") setStepState("Sandbox", "active"); // sandbox prep emits no agent_start (SPEC.md: plain code, zero agent calls)
      persistRun(data.run_id, numRounds, mode);
      connect(data.run_id);
    } catch (err) {
      showError(`Network error starting run: ${err.message}`);
      runBtn.disabled = false;
    }
  }

  function encodePathForUrl(path) {
    return path.split("/").map(encodeURIComponent).join("/");
  }

  function sortFiles(files) {
    return [...files].sort((a, b) => {
      const pa = FILE_PRIORITY.indexOf(a);
      const pb = FILE_PRIORITY.indexOf(b);
      if (pa !== -1 || pb !== -1) {
        if (pa === -1) return 1;
        if (pb === -1) return -1;
        return pa - pb;
      }
      return a.localeCompare(b);
    });
  }

  async function selectFile(path) {
    state.selectedFile = path;
    for (const li of fileListEl.children) {
      li.classList.toggle("selected", li.dataset.path === path);
    }
    fileViewEl.textContent = "Loading…";
    try {
      const res = await fetch(`/output/${state.runId}/${encodePathForUrl(path)}`);
      if (!res.ok) {
        fileViewEl.textContent = `Error ${res.status} loading ${path}`;
        return;
      }
      fileViewEl.textContent = await res.text();
    } catch (err) {
      fileViewEl.textContent = `Error loading file: ${err.message}`;
    }
  }

  function renderFileList(files) {
    fileListEl.replaceChildren();
    const sorted = sortFiles(files);
    fileCountEl.textContent = `${sorted.length} File${sorted.length === 1 ? "" : "s"}`;
    for (const path of sorted) {
      const li = document.createElement("li");
      li.textContent = path;
      li.dataset.path = path;
      if (path === state.selectedFile) li.classList.add("selected");
      li.addEventListener("click", () => selectFile(path));
      fileListEl.appendChild(li);
    }
  }

  async function refreshFiles() {
    if (!state.runId) return;
    try {
      const res = await fetch(`/output/${state.runId}`);
      if (!res.ok) return;
      const data = await res.json();
      renderFileList(data.files || []);
    } catch {
      // transient network hiccup; next files_updated/phase_done will retry
    }
  }

  async function tryRecoverRun() {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    let saved;
    try {
      saved = JSON.parse(raw);
    } catch {
      clearPersistedRun();
      return;
    }
    const { runId, numRounds, mode } = saved || {};
    if (!runId) {
      clearPersistedRun();
      return;
    }
    let res;
    try {
      res = await fetch(`/status/${runId}`);
    } catch {
      return; // network hiccup; leave the stored run alone, user can retry later
    }
    if (res.status === 404) {
      clearPersistedRun();
      return;
    }
    if (!res.ok) return;
    const status = await res.json();

    state.runId = runId;
    state.numRounds = numRounds || 3;
    state.mode = mode || "full";
    selectPill("mode", state.mode);
    applyModeVisibility(state.mode);
    updateConfigSummary();
    runBtn.disabled = !!status.running;
    cancelBtn.hidden = !status.running;
    runIdChipEl.textContent = runId;
    // The SSE replay buffer (connect() below) re-delivers every historical event for
    // this run, and handleEvent()/updateHud() rebuild the tracker/cards/cost-tag from
    // that replay for free -- no separate "reconstruct state from /status" logic needed,
    // same reasoning as the pre-Stage-13 progress bar.
    connect(runId);
    if (status.running) {
      setConfigCollapsed(true);
      runPhaseLabelEl.textContent = "Recovering…";
      runDotEl.className = "dot active pulse";
      if (status.paused) showPaused(status.paused_until);
    } else {
      refreshFiles();
      setConfigCollapsed(false);
      runPhaseLabelEl.textContent = runDoneLabel(status.error);
      runDotEl.className = status.error ? "dot error" : "dot success";
      clearPersistedRun();
    }
    if (status.error) showError(status.error);
  }

  runBtn.addEventListener("click", startRun);
  cancelBtn.addEventListener("click", cancelRun);
  roundsInput.addEventListener("input", updateConfigSummary);
  configToggleEl.addEventListener("click", () => {
    setConfigCollapsed(configToggleEl.getAttribute("aria-expanded") === "true");
  });
  wirePillGroup("mode", (value) => {
    applyModeVisibility(value);
    updateConfigSummary();
  });
  wirePillGroup("model", () => {
    scheduleModelCheck();
    updateConfigSummary();
  });
  wirePillGroup("effort", updateConfigSummary);
  window.addEventListener("DOMContentLoaded", () => {
    applyModeVisibility(getSelectedPillValue("mode"));
    loadUiConfig();
    tryRecoverRun();
  });
})();
