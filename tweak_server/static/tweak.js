/* OpenMontage tweak UI — minimal vanilla JS
 *
 * Reads window.__PROJECT_ID__ (injected by server) and the template JSON from
 * GET /api/projects/{id}. Builds per-cut fields based on cut type, submits
 * the diff via POST /api/projects/{id}/tweak, shows result.
 */

(function () {
  "use strict";

  const PROJECT_ID = window.__PROJECT_ID__ || "";
  // Bearer token (set via cookie or prompt). Empty string = no auth required
  // (development mode — server logs a warning if TWEAK_SERVER_BEARER unset).
  const TOKEN = localStorage.getItem("tweak_token") || "";

  const els = {
      projectLabel: document.getElementById("project-id-label"),
      theme: document.getElementById("theme"),
      cuts: document.getElementById("cuts-container"),
      audio: document.getElementById("audio-container"),
      comment: document.getElementById("comment"),
      submit: document.getElementById("submit-btn"),
      submitStatus: document.getElementById("submit-status"),
      resultCard: document.getElementById("result-card"),
      resultSummary: document.getElementById("result-summary"),
      resultVideo: document.getElementById("result-video"),
      resultError: document.getElementById("result-error"),
      progressBlock: document.getElementById("progress-block"),
      progressBar: document.getElementById("progress-bar"),
      progressPhase: document.getElementById("progress-phase"),
    };

  // Active SSE connection (set while a render is in-flight). We keep the
  // reference so we can close it on retry / new submit.
  let ACTIVE_EVENT_SOURCE = null;

  let SCHEMA = null;        // {themes, animations, field_ranges, ...}
  let TEMPLATE = null;      // current props JSON
  let CUT_DOMS = [];        // per-cut DOM refs for collectPayload()

  // -------------------------------------------------------------------------
  // Boot
  // -------------------------------------------------------------------------
  init().catch((err) => {
    setStatus("Boot error: " + (err.message || err), "error");
    console.error(err);
  });

  async function init() {
    els.projectLabel.textContent = `(project: ${PROJECT_ID})`;

    const resp = await fetchJSON(`/api/projects/${encodeURIComponent(PROJECT_ID)}`);
    SCHEMA = resp;
    TEMPLATE = resp.template;

    populateThemes(resp.themes);
    buildCuts(resp.template.cuts || []);
    buildAudio(resp.template.audio || {});
    bindSubmit();
  }

  // -------------------------------------------------------------------------
  // Field builders
  // -------------------------------------------------------------------------

  function populateThemes(themes) {
    els.theme.innerHTML = "";
    for (const t of themes) {
      const opt = document.createElement("option");
      opt.value = t;
      opt.textContent = t;
      if (TEMPLATE.theme === t) opt.selected = true;
      els.theme.appendChild(opt);
    }
  }

  function buildCuts(cuts) {
    els.cuts.innerHTML = "";
    CUT_DOMS = [];
    cuts.forEach((cut, idx) => {
      const isText = ["text_card", "hero_title", "stat_card", "callout", "section_title"]
        .includes(cut.type);
      const isImageVideo = !cut.type && cut.source;

      const wrap = document.createElement("div");
      wrap.className = "cut";

      // Header
      const header = document.createElement("div");
      header.className = "cut-header";
      const idEl = document.createElement("span");
      idEl.className = "cut-id";
      idEl.textContent = cut.id || `cut-${idx}`;
      const typeEl = document.createElement("span");
      typeEl.className = "cut-type";
      typeEl.textContent = cut.type || (isImageVideo ? (cut.source ? "image/video" : "unknown") : "unknown");
      header.append(idEl, typeEl);
      wrap.appendChild(header);

      const row = document.createElement("div");
      row.className = "cut-row";

      // Universal: in/out seconds
      row.appendChild(makeNumberInput("in_seconds", "in (s)", cut.in_seconds, 0, 600, 0.1));
      row.appendChild(makeNumberInput("out_seconds", "out (s)", cut.out_seconds, 0, 600, 0.1));
      row.appendChild(makeColorInput("backgroundColor", "bg color", cut.backgroundColor));

      const refs = {
        id: cut.id,
        in_seconds: row.querySelector('[data-field="in_seconds"]'),
        out_seconds: row.querySelector('[data-field="out_seconds"]'),
        backgroundColor: row.querySelector('[data-field="backgroundColor"]'),
      };

      if (isText) {
        row.appendChild(makeTextInput("text", "text", cut.text));
        row.appendChild(makeNumberInput("fontSize", "font size", cut.fontSize, 24, 200, 1));
        row.appendChild(makeColorInput("color", "text color", cut.color));
        refs.text = row.querySelector('[data-field="text"]');
        refs.fontSize = row.querySelector('[data-field="fontSize"]');
        refs.color = row.querySelector('[data-field="color"]');
      } else if (isImageVideo) {
        const sel = makeSelect("animation", "animation",
          ["zoom-in", "pan-down", "ken-burns", "none"], cut.animation);
        row.appendChild(sel);
        refs.animation = row.querySelector('[data-field="animation"]');
      }

      wrap.appendChild(row);
      els.cuts.appendChild(wrap);
      CUT_DOMS.push(refs);
    });
  }

  function buildAudio(audio) {
    els.audio.innerHTML = "";
    const blocks = ["narration", "music"];
    for (const block of blocks) {
      const data = audio[block] || {};
      const h3 = document.createElement("h3");
      h3.textContent = block;
      h3.style.fontSize = "13px";
      h3.style.margin = "12px 0 8px";
      h3.style.color = "var(--accent)";
      els.audio.appendChild(h3);

      const wrap = document.createElement("div");
      wrap.className = "cut-row";

      const volumeEl = makeRange("volume", "volume", data.volume ?? 0.5, 0, 1, 0.01);
      volumeEl.querySelector("input").dataset.block = block;
      wrap.appendChild(volumeEl);

      if (block === "music") {
        for (const [field, label, def, max] of [
          ["fadeInSeconds",  "fade in (s)",  data.fadeInSeconds  ?? 0.5, 3],
          ["fadeOutSeconds", "fade out (s)", data.fadeOutSeconds ?? 0.5, 3],
          ["offsetSeconds",  "offset (s)",   data.offsetSeconds  ?? 0,   30],
        ]) {
          const el = makeNumberInput(field, label, data[field] ?? def, 0, max, 0.1);
          el.querySelector("input").dataset.block = block;
          wrap.appendChild(el);
        }
      }
      els.audio.appendChild(wrap);
    }
    // Build a flat lookup map {block: {field: inputElement}} for collectPayload.
    const refs = {};
    els.audio.querySelectorAll("input[data-field]").forEach((inp) => {
      const b = inp.dataset.block;
      const f = inp.dataset.field;
      if (b && f) {
        refs[b] = refs[b] || {};
        refs[b][f] = inp;
      }
    });
    els.audio._refs = refs;
  }

  function makeRange(field, label, value, min, max, step) {
    const wrap = document.createElement("label");
    wrap.className = "inline";
    const span = document.createElement("span");
    span.textContent = label;
    const out = document.createElement("span");
    out.className = "value-display";
    out.textContent = (value * 100).toFixed(0) + "%";
    out.style.minWidth = "3em";
    out.style.textAlign = "right";
    out.style.color = "var(--accent)";
    const inp = document.createElement("input");
    inp.type = "range";
    inp.min = min; inp.max = max; inp.step = step;
    inp.value = value;
    inp.dataset.field = field;
    inp.addEventListener("input", () => {
      out.textContent = (parseFloat(inp.value) * 100).toFixed(0) + "%";
    });
    wrap.append(span, inp, out);
    return wrap;
  }

  function makeNumberInput(field, label, value, min, max, step) {
    const wrap = document.createElement("label");
    const span = document.createElement("span");
    span.textContent = label;
    const inp = document.createElement("input");
    inp.type = "number";
    inp.value = value ?? "";
    inp.min = min; inp.max = max; inp.step = step;
    inp.dataset.field = field;
    wrap.append(span, inp);
    return wrap;
  }

  function makeTextInput(field, label, value) {
    const wrap = document.createElement("label");
    const span = document.createElement("span");
    span.textContent = label;
    const inp = document.createElement("input");
    inp.type = "text";
    inp.value = value ?? "";
    inp.maxLength = 500;
    inp.dataset.field = field;
    wrap.append(span, inp);
    return wrap;
  }

  function makeColorInput(field, label, value) {
    const wrap = document.createElement("label");
    const span = document.createElement("span");
    span.textContent = label;
    const inp = document.createElement("input");
    inp.type = "color";
    inp.value = value && /^#([0-9A-F]{6}|[0-9A-F]{8})$/.test(value)
      ? value.slice(0, 7)
      : "#000000";
    inp.dataset.field = field;
    wrap.append(span, inp);
    return wrap;
  }

  function makeSelect(field, label, options, value) {
    const wrap = document.createElement("label");
    const span = document.createElement("span");
    span.textContent = label;
    const sel = document.createElement("select");
    sel.dataset.field = field;
    for (const opt of options) {
      const o = document.createElement("option");
      o.value = opt; o.textContent = opt;
      if (opt === value) o.selected = true;
      sel.appendChild(o);
    }
    wrap.append(span, sel);
    return wrap;
  }

  // -------------------------------------------------------------------------
  // Submit
  // -------------------------------------------------------------------------

  function bindSubmit() {
    els.submit.addEventListener("click", onSubmit);
  }

  async function onSubmit() {
    els.submit.disabled = true;
    setStatus("Rendering… (this can take 30-90s for a 60s video)", "");
    hideResult();
    closeActiveStream();

    const payload = collectPayload();
    if (!payload) {
      els.submit.disabled = false;
      return;
    }

    try {
      const resp = await fetchJSON(`/api/projects/${encodeURIComponent(PROJECT_ID)}/tweak`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      // Async mode: backend returns {job_id, status:"queued", ...} and the
      // progress stream lives at /jobs/{job_id}/events. Fall back to the
      // old synchronous result path if the backend hasn't migrated yet.
      if (resp && resp.job_id) {
        await followJob(resp.job_id, resp);
      } else {
        showResult(resp);
        setStatus(
          resp.success
            ? `Rendered in ${resp.duration_seconds}s → ${resp.output_path}`
            : `Render failed: ${resp.error}`,
          resp.success ? "ok" : "error"
        );
      }
    } catch (err) {
      console.error(err);
      setStatus("Error: " + (err.message || err), "error");
      showResultError(err.message || String(err));
    } finally {
      els.submit.disabled = false;
    }
  }

  // -------------------------------------------------------------------------
  // Progress stream (SSE) — open EventSource on the per-job endpoint, update
  // the progress bar + phase text. On terminal phase, fall back to a single
  // GET /jobs/{id} to get the final state (output_path, error).
  // -------------------------------------------------------------------------

  function showProgress() {
    if (els.progressBlock) els.progressBlock.classList.remove("hidden");
    setProgress(0, "queued");
  }

  function hideProgress() {
    if (els.progressBlock) els.progressBlock.classList.add("hidden");
  }

  function setProgress(percent, phase) {
    if (els.progressBar) {
      const v = Math.max(0, Math.min(100, Number(percent) || 0));
      els.progressBar.value = v;
    }
    if (els.progressPhase && phase) {
      els.progressPhase.textContent = phase;
    }
  }

  function closeActiveStream() {
    if (ACTIVE_EVENT_SOURCE) {
      try { ACTIVE_EVENT_SOURCE.close(); } catch (_) { /* noop */ }
      ACTIVE_EVENT_SOURCE = null;
    }
  }

  async function followJob(jobId, initialResp) {
    showProgress();
    if (els.resultCard) els.resultCard.classList.remove("hidden");
    if (els.resultSummary) els.resultSummary.textContent = "Rendering…";
    if (els.resultVideo) { els.resultVideo.removeAttribute("src"); els.resultVideo.load(); }
    if (els.resultError) els.resultError.textContent = "";

    // Browser EventSource can't send custom headers — token must travel in
    // the URL when called from the browser directly. The tweak server's
    // /jobs/{id}/events endpoint still requires X-Tweak-Token via the
    // require_token dependency. For same-origin deploys, the browser sends
    // no auth and the server runs with TWEAK_SERVER_BEARER unset; in
    // production a same-host reverse proxy injects the header. If the token
    // is in localStorage we append it as ?token= for browser-only auth.
    const url = new URL(
      `/api/projects/${encodeURIComponent(PROJECT_ID)}/jobs/${encodeURIComponent(jobId)}/events`,
      window.location.origin,
    );
    if (TOKEN) url.searchParams.set("token", TOKEN);

    await new Promise((resolve) => {
      const es = new EventSource(url.toString());
      ACTIVE_EVENT_SOURCE = es;
      let settled = false;

      const finish = () => {
        if (settled) return;
        settled = true;
        try { es.close(); } catch (_) { /* noop */ }
        if (ACTIVE_EVENT_SOURCE === es) ACTIVE_EVENT_SOURCE = null;
        resolve();
      };

      es.addEventListener("error", () => {
        // Browser fires 'error' on stream close too — only stop if the
        // stream is actually closed OR the connection failed.
        if (es.readyState === EventSource.CLOSED) finish();
      });

      es.addEventListener("message", (ev) => {
        let data = null;
        try { data = JSON.parse(ev.data); } catch (_) { return; }
        if (!data || typeof data !== "object") return;
        const phase = data.phase || data.status;
        const percent = data.percent;
        if (typeof percent === "number") setProgress(percent, phase);
        else if (phase) setProgress(els.progressBar ? els.progressBar.value : 0, phase);
        if (data.message && els.progressPhase) {
          els.progressPhase.textContent = `${phase || ""}${data.message ? " — " + data.message : ""}`.trim();
        }
        if (phase === "completed" || phase === "failed") finish();
      });

      // Named event the backend may emit on failure (we also synthesise this
      // from progress.py when MCP returns non-200).
      es.addEventListener("error_event", () => finish());
    });

    // After the stream closes, fetch the final state to populate the result
    // card with output_path / error.
    try {
      const final = await fetchJSON(
        `/api/projects/${encodeURIComponent(PROJECT_ID)}/jobs/${encodeURIComponent(jobId)}`,
      );
      showResult({
        success: final.status === "completed",
        project_id: final.project_id,
        staging_id: final.staging_id,
        output_path: final.output_path,
        duration_seconds: null,
        error: final.error,
        decision_log: null,
        comment: null,
        merged_cuts_touched: [],
      });
      setStatus(
        final.status === "completed"
          ? `Rendered → ${final.output_path || "(no output path)"}`
          : `Render failed: ${final.error || "unknown"}`,
        final.status === "completed" ? "ok" : "error",
      );
      if (final.status === "completed" || final.status === "failed") hideProgress();
    } catch (err) {
      console.error("final state fetch failed:", err);
      setStatus("Stream closed; final state unavailable", "error");
      hideProgress();
    }
  }

  function collectPayload() {
    const payload = {
      theme: els.theme.value || undefined,
      cuts: [],
      audio: {},
      comment: els.comment.value || "",
    };
    for (const refs of CUT_DOMS) {
      const cut = { id: refs.id };
      for (const k of ["in_seconds", "out_seconds", "backgroundColor", "text",
                       "fontSize", "color", "animation"]) {
        const inp = refs[k];
        if (!inp) continue;
        const v = inp.value;
        if (v === "" || v == null) continue;
        cut[k] = k.endsWith("seconds") || k === "fontSize" ? Number(v) : v;
      }
      payload.cuts.push(cut);
    }
    // Audio
    const a = els.audio._refs;
    if (a) {
      const narration = {};
      if (a.narration.volume && a.narration.volume.value !== "")
        narration.volume = Number(a.narration.volume.value);
      if (Object.keys(narration).length) payload.audio.narration = narration;

      const music = {};
      if (a.music.volume && a.music.volume.value !== "")
        music.volume = Number(a.music.volume.value);
      if (a.music.fadeInSeconds && a.music.fadeInSeconds.value !== "")
        music.fadeInSeconds = Number(a.music.fadeInSeconds.value);
      if (a.music.fadeOutSeconds && a.music.fadeOutSeconds.value !== "")
        music.fadeOutSeconds = Number(a.music.fadeOutSeconds.value);
      if (a.music.offsetSeconds && a.music.offsetSeconds.value !== "")
        music.offsetSeconds = Number(a.music.offsetSeconds.value);
      if (Object.keys(music).length) payload.audio.music = music;
    }
    if (Object.keys(payload.audio).length === 0) delete payload.audio;
    return payload;
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  async function fetchJSON(url, opts = {}) {
    const headers = Object.assign({}, opts.headers || {});
    if (TOKEN) headers["X-Tweak-Token"] = TOKEN;
    const resp = await fetch(url, Object.assign({}, opts, { headers }));
    if (!resp.ok) {
      let body;
      try { body = await resp.json(); } catch { body = await resp.text(); }
      const detail = typeof body === "object" && body.detail
        ? JSON.stringify(body.detail)
        : String(body);
      throw new Error(`HTTP ${resp.status}: ${detail}`);
    }
    return resp.json();
  }

  function setStatus(msg, kind) {
    els.submitStatus.textContent = msg || "";
    els.submitStatus.className = "status" + (kind ? " " + kind : "");
  }

  function hideResult() {
    els.resultCard.classList.add("hidden");
    els.resultVideo.removeAttribute("src");
    els.resultVideo.load();
    els.resultError.textContent = "";
    if (els.progressBlock) els.progressBlock.classList.add("hidden");
    if (els.progressBar) els.progressBar.value = 0;
    if (els.progressPhase) els.progressPhase.textContent = "queued";
  }

  function showResult(resp) {
    els.resultCard.classList.remove("hidden");
    if (resp.success) {
      els.resultSummary.textContent =
        `✓ Rendered to ${resp.output_path} in ${resp.duration_seconds}s ` +
        `(decision log: ${resp.decision_log})`;
      els.resultVideo.src = "/renders/" + PROJECT_ID + "/" +
        (resp.output_path.split("/").pop());
      els.resultError.textContent = "";
    } else {
      els.resultSummary.textContent = "✗ Render failed";
      els.resultError.textContent = resp.error || "unknown error";
    }
  }

  function showResultError(msg) {
    els.resultCard.classList.remove("hidden");
    els.resultSummary.textContent = "✗ Render failed";
    els.resultError.textContent = msg;
  }
})();