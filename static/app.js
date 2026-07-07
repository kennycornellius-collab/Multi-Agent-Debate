(() => {
  "use strict";

  const STORAGE_KEY = "debate-pipeline-run";
  const BUILD_ORDER = ["Architect", "Coder", "Reviewer", "Tester"];
  const FILE_PRIORITY = ["agreed_spec.md", "architecture.md", "review.md", "tests.md"];
  const SCROLL_BOTTOM_THRESHOLD = 40;
  const MODEL_CHECK_DEBOUNCE_MS = 400;

  const ideaInput = document.getElementById("idea");
  const roundsInput = document.getElementById("rounds");
  const runBtn = document.getElementById("run-btn");
  const runErrorEl = document.getElementById("run-error");
  const modelSelect = document.getElementById("model-select");
  const modelStatusEl = document.getElementById("model-status");
  const effortSlider = document.getElementById("effort-slider");
  const effortLabelEl = document.getElementById("effort-label");
  const progressFill = document.getElementById("progress-fill");
  const progressLabel = document.getElementById("progress-label");
  const debateFeedBody = document.querySelector("#debate-feed .feed-body");
  const buildFeedBody = document.querySelector("#build-feed .feed-body");
  const fileListEl = document.getElementById("file-list");
  const fileViewEl = document.getElementById("file-view");

  const state = {
    runId: null,
    numRounds: 3,
    eventSource: null,
    cards: new Map(), // key -> { el, body, status }
    selectedFile: null,
    // effortChoices[0] is always "" (Default); effortChoices[i] for i>0 is a real
    // --effort value, in the order config.py's AVAILABLE_EFFORT_LEVELS lists them.
    effortChoices: [""],
    modelCheckSeq: 0, // guards against a stale check response overwriting a newer one
  };

  function clamp(n, lo, hi) {
    return Math.max(lo, Math.min(hi, n));
  }

  function showError(msg) {
    runErrorEl.textContent = msg;
    runErrorEl.hidden = false;
  }

  function hideError() {
    runErrorEl.hidden = true;
    runErrorEl.textContent = "";
  }

  function setProgress(fraction, label) {
    progressFill.style.width = `${clamp(fraction, 0, 1) * 100}%`;
    progressLabel.textContent = label;
  }

  function persistRun(runId, numRounds) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ runId, numRounds }));
  }

  function clearPersistedRun() {
    localStorage.removeItem(STORAGE_KEY);
  }

  function capitalize(s) {
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function currentEffort() {
    return state.effortChoices[parseInt(effortSlider.value, 10)] || "";
  }

  function updateEffortLabel() {
    const effort = currentEffort();
    effortLabelEl.textContent = effort ? capitalize(effort) : "Default";
  }

  async function loadUiConfig() {
    try {
      const res = await fetch("/config");
      if (!res.ok) return;
      const data = await res.json();
      for (const model of data.models || []) {
        const opt = document.createElement("option");
        opt.value = model;
        opt.textContent = capitalize(model);
        modelSelect.appendChild(opt);
      }
      state.effortChoices = ["", ...(data.effort_levels || [])];
      effortSlider.max = String(state.effortChoices.length - 1);
    } catch {
      // /config unreachable: dropdown just stays at "Default" and the slider covers
      // only the Default position -- run() still works, just without overrides.
    }
    updateEffortLabel();
  }

  function setModelStatus(text, cls) {
    modelStatusEl.textContent = text;
    modelStatusEl.className = `model-status${cls ? ` ${cls}` : ""}`;
  }

  let modelCheckTimer = null;

  function scheduleModelCheck() {
    const model = modelSelect.value;
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
      if (seq !== state.modelCheckSeq || modelSelect.value !== model) return; // stale
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
      if (seq !== state.modelCheckSeq || modelSelect.value !== model) return;
      setModelStatus(`network error: ${err.message}`, "unavailable");
    }
  }

  function feedBodyFor(phase) {
    return phase === "debate" ? debateFeedBody : buildFeedBody;
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

  function getOrCreateCard(ev) {
    const key = cardKey(ev);
    let card = state.cards.get(key);
    if (card) return card;

    const el = document.createElement("div");
    el.className = `card agent-${(ev.agent || "system").toLowerCase()}`;

    const header = document.createElement("div");
    header.className = "card-header";

    const nameSpan = document.createElement("span");
    nameSpan.textContent = ev.agent || "system";
    if (ev.phase === "debate") {
      const roundSpan = document.createElement("span");
      roundSpan.className = "card-round";
      roundSpan.textContent = ev.round == null ? " · final synthesis" : ` · round ${ev.round}`;
      nameSpan.appendChild(roundSpan);
    }

    const statusSpan = document.createElement("span");
    statusSpan.className = "card-status";
    statusSpan.textContent = "streaming…";

    header.appendChild(nameSpan);
    header.appendChild(statusSpan);

    const body = document.createElement("div");
    body.className = "card-body";

    el.appendChild(header);
    el.appendChild(body);

    const container = feedBodyFor(ev.phase);
    withAutoScroll(container, () => container.appendChild(el));

    card = { el, body, status: statusSpan };
    state.cards.set(key, card);
    return card;
  }

  function updateProgress(ev) {
    if (ev.type === "run_done") {
      setProgress(1, ev.content ? "Finished with errors" : "Done");
      return;
    }
    if (ev.type === "phase_done") {
      if (ev.phase === "debate") setProgress(0.5, "Build starting…");
      else if (ev.phase === "build") setProgress(1, "Build complete");
      return;
    }
    if (ev.type !== "agent_start") return;
    if (ev.phase === "debate") {
      const n = state.numRounds || 3;
      const within = ev.round == null ? 1 : (ev.round - 1) / n;
      setProgress(
        0.5 * Math.min(within, 1),
        ev.round == null ? `Debate — final synthesis (${ev.agent})` : `Debate — round ${ev.round}/${n}: ${ev.agent}`
      );
    } else if (ev.phase === "build") {
      const idx = BUILD_ORDER.indexOf(ev.agent);
      const within = idx === -1 ? 0 : idx / BUILD_ORDER.length;
      setProgress(0.5 + 0.5 * within, `Build — ${ev.agent}`);
    }
  }

  function handleEvent(ev) {
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
          card.status.textContent = "streaming…";
          card.el.classList.remove("error");
        }
        updateProgress(ev);
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
        card.status.textContent = ev.content ? `done (${ev.content})` : "done";
        break;
      }
      case "error": {
        const card = getOrCreateCard(ev);
        card.el.classList.add("error");
        card.status.textContent = "error";
        if (ev.content) card.body.textContent += `\n[error] ${ev.content}`;
        break;
      }
      case "files_updated":
        refreshFiles();
        break;
      case "phase_done":
        updateProgress(ev);
        refreshFiles();
        break;
      case "run_done":
        updateProgress(ev);
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
    clearPersistedRun();
    if (content) showError(content);
    refreshFiles();
  }

  function resetFeeds() {
    debateFeedBody.replaceChildren();
    buildFeedBody.replaceChildren();
    state.cards.clear();
    fileListEl.replaceChildren();
    fileViewEl.textContent = "Select a file to view its contents.";
    state.selectedFile = null;
    setProgress(0, "Starting…");
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
    const idea = ideaInput.value.trim();
    if (!idea) {
      showError("Please enter a project idea.");
      return;
    }
    const numRounds = clamp(parseInt(roundsInput.value, 10) || 3, 1, 8);
    const model = modelSelect.value || null;
    const effort = currentEffort() || null;

    runBtn.disabled = true;
    try {
      const res = await fetch("/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idea, num_rounds: numRounds, agents: [], model, effort }),
      });
      if (!res.ok) {
        showError((await safeDetail(res)) || `Request failed (${res.status})`);
        runBtn.disabled = false;
        return;
      }
      const data = await res.json();
      resetFeeds();
      state.numRounds = numRounds;
      persistRun(data.run_id, numRounds);
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
    for (const path of sortFiles(files)) {
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
    const { runId, numRounds } = saved || {};
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
    runBtn.disabled = !!status.running;
    connect(runId);
    if (status.running) {
      setProgress(0, "Recovering…");
    } else {
      refreshFiles();
      setProgress(1, status.error ? "Finished with errors" : "Done");
      clearPersistedRun();
    }
    if (status.error) showError(status.error);
  }

  runBtn.addEventListener("click", startRun);
  modelSelect.addEventListener("change", scheduleModelCheck);
  effortSlider.addEventListener("input", updateEffortLabel);
  window.addEventListener("DOMContentLoaded", () => {
    loadUiConfig();
    tryRecoverRun();
  });
})();
