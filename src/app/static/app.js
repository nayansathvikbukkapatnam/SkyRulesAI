/* SkyRules AI page logic (docs/UI_SPEC.md). Plain JavaScript, no build step.

   POST /api/ask streams server-sent events (src/app/server.py):
     stage  {name, status, ms, config}   status: started | done | stopped | skipped
     answer the Answer from answer(), plus config ("b3", or "b2" in compare mode)
     error  {message}
     done   {}
   Open with ?mock=1 to replay recorded events from /static/mock_events.json instead.

   Every string from the server is inserted with textContent, never as HTML: answers
   and passages come from a model and from web pages, so they are untrusted. */
(function () {
  "use strict";

  // --- Wording (UI_SPEC section 4) ---------------------------------------------------------
  const REFUSAL_TEXT = "I don't have that information in the official sources I can search.";
  const REFUSAL_HINT =
    "SkyRules only answers from the federal drone regulations, FAA rule documents and Know Before You Fly guidance, " +
    "so it won't guess. Try a question about registration, airspace, Remote ID or night flying.";
  const ERROR_TEXT = "Something went wrong, so SkyRules did not answer. Try again.";
  const B4UFLY_URL = "https://www.faa.gov/uas/getting_started/b4ufly";
  const FACILITY_MAPS_URL = "https://www.faa.gov/uas/commercial_operators/uas_facility_maps";
  const FAA_UAS_URL = "https://www.faa.gov/uas";

  const STAGES = ["route", "retrieve", "rerank", "generate", "check"];
  const STAGE_LABELS = {
    route: "Understanding question",
    retrieve: "Searching 20 passages",
    rerank: "Reranking to top 5",
    generate: "Writing answer",
    check: "Checking safeguards",
  };
  const CHECK_LABELS = {
    citation_validity: "Citations",
    claim_support: "Claims",
    number_date: "Numbers",
    quote_limit: "Quotes",
  };
  const PILOT_NAMES = {
    recreational: "Recreational flyer",
    part107: "Part 107 pilot",
    not_sure: "Not sure, covering both",
  };
  const TAG_LABELS = {
    location_live: "Location-specific",
    high_stakes: "High-stakes",
    pilot_unclear: "Pilot type unclear",
    injection: "Instructions removed",
  };
  const PAGE_TYPES = {
    guide_page: "Guide page",
    faq: "FAQ",
    regulation_summary: "Regulation",
    advisory_circular: "Advisory circular",
    study_guide: "Study guide",
    toolkit_pdf: "PDF",
    data_list: "Data list",
  };
  const FIGURE_UNITS = /(\d[\d,]*(?:\.\d+)?)\s*(feet|foot|ft|pounds|pound|lbs|lb|grams|gram|g|mph|knots|knot|miles|mile|days|day|months|month|years|year)\b/i;
  const MAX_TURNS = 6;
  const REVEAL_MS = 380;

  const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const params = new URLSearchParams(window.location.search);

  const state = {
    pilot: "not_sure",
    compare: false,
    mock: params.get("mock") === "1",
    busy: false,
    traceOpen: false,
    turns: [], // finished turns: {question, assistant, summary}
    current: null, // the turn being shown
  };

  // --- Small DOM helpers ------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);

  function el(tag, props, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of [].concat(children || [])) {
      if (child === null || child === undefined || child === false) continue;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    }
    return node;
  }

  // Constant icon markup only (never data from the server).
  function icon(markup) {
    const t = document.createElement("template");
    t.innerHTML = markup.trim();
    return t.content.firstChild;
  }
  const ICONS = {
    done: '<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="9" fill="#1E7A46"></circle><path d="M6 10.5l2.6 2.6L14 7.6" fill="none" stroke="#FFFFFF" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path></svg>',
    active: '<svg class="sr-spinner" width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="8" fill="none" stroke="#F3D3C2" stroke-opacity="0.6" stroke-width="3"></circle><path d="M10 2a8 8 0 0 1 8 8" fill="none" stroke="#D9480F" stroke-width="3" stroke-linecap="round"></path></svg>',
    pending: '<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="8" fill="none" stroke="#9AA9BC" stroke-width="2"></circle></svg>',
    stopped: '<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="9" fill="#D9480F"></circle><path d="M10 5.5v5.5M10 14v.5" fill="none" stroke="#FFFFFF" stroke-width="2.2" stroke-linecap="round"></path></svg>',
    check: '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M3 7.4l2.4 2.4L11 4.4" fill="none" stroke="#2E9E5E" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path></svg>',
    cross: '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M3.5 3.5l7 7M10.5 3.5l-7 7" fill="none" stroke="#D9480F" stroke-width="2" stroke-linecap="round"></path></svg>',
    warn: '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1.5l4.8 8.5H1.2z" fill="none" stroke="#D9480F" stroke-width="1.3" stroke-linejoin="round"></path></svg>',
    referral: '<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><path d="M10 2.5l8 14H2z" fill="none" stroke="#D9480F" stroke-width="1.8" stroke-linejoin="round"></path><path d="M10 8v4M10 14.5v.3" stroke="#D9480F" stroke-width="1.8" stroke-linecap="round"></path></svg>',
    refusal: '<svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><circle cx="14" cy="14" r="12.5" fill="#D9480F" fill-opacity="0.14" stroke="#D9480F" stroke-width="1.5"></circle><path d="M14 8v7M14 19.5v.3" stroke="#D9480F" stroke-width="2.4" stroke-linecap="round"></path></svg>',
    drone: '<svg width="40" height="28" viewBox="0 0 40 28" aria-hidden="true"><rect x="14" y="10" width="12" height="8" rx="3" fill="#D9480F"></rect><line x1="15" y1="12" x2="6" y2="6" stroke="#7F90A4" stroke-width="2" stroke-linecap="round"></line><line x1="25" y1="12" x2="34" y2="6" stroke="#7F90A4" stroke-width="2" stroke-linecap="round"></line><ellipse class="sr-rotor" cx="6" cy="5" rx="6" ry="1.6" fill="#4C8BD6" fill-opacity="0.7"></ellipse><ellipse class="sr-rotor" cx="34" cy="5" rx="6" ry="1.6" fill="#4C8BD6" fill-opacity="0.7"></ellipse></svg>',
  };

  function safeUrl(url) {
    return typeof url === "string" && /^https?:\/\//i.test(url) ? url : null;
  }

  function seconds(ms) {
    return (Math.max(0, ms || 0) / 1000).toFixed(2) + " s";
  }

  function announce(text) {
    $("live").textContent = text;
  }

  // --- Theme and controls -------------------------------------------------------------------
  function storeGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function storeSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* private mode: ignore */ }
  }

  function setTheme(dark) {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    $("theme-btn").setAttribute("aria-pressed", dark ? "true" : "false");
    $("theme-label").textContent = dark ? "Daylight mode" : "Cockpit mode";
    $("theme-moon").hidden = dark;
    $("theme-sun").hidden = !dark;
    storeSet("skyrules-theme", dark ? "dark" : "light");
  }

  function setPilot(pilot) {
    state.pilot = pilot;
    for (const b of document.querySelectorAll(".pilot-btn")) {
      b.setAttribute("aria-pressed", b.dataset.pilot === pilot ? "true" : "false");
    }
  }

  function setCompare(on) {
    state.compare = on;
    $("compare-btn").setAttribute("aria-pressed", on ? "true" : "false");
  }

  // --- Event stream ---------------------------------------------------------------------------
  function parseBlock(block) {
    let event = "message";
    const data = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    return { event, data: data.length ? JSON.parse(data.join("\n")) : {} };
  }

  async function streamAsk(body, onEvent) {
    if (state.mock) return replayMock(body, onEvent);
    const response = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok || !response.body) throw new Error("the server answered " + response.status);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        if (block.trim()) onEvent(parseBlock(block));
      }
    }
  }

  let mockData = null;
  async function replayMock(body, onEvent) {
    if (!mockData) mockData = await (await fetch("/static/mock_events.json")).json();
    const q = body.question.toLowerCase();
    let key = "answer";
    if (/(buy|purchase|best drone|which drone)/.test(q)) key = "refusal";
    else if (/(san diego|tonight|near me|right now|\bhere\b)/.test(q)) key = "location";
    const events = mockData.scenarios[key].filter((e) => body.compare || e.data.config !== "b2");
    for (const e of events) {
      await new Promise((r) => setTimeout(r, e.event === "stage" && e.data.status === "done" ? 320 : 40));
      onEvent(e);
    }
  }

  // --- Turn view -------------------------------------------------------------------------------
  function newTurn(question) {
    const turn = {
      question,
      started: performance.now(),
      timer: null,
      stages: Object.fromEntries(STAGES.map((s) => [s, { status: "pending", ms: 0 }])),
      answer: null,
      b2: null,
      error: null,
      finished: false,
      revealTimers: [],
      nodes: {},
    };
    const n = turn.nodes;
    n.rewrite = el("span", { class: "rewrite", hidden: true });
    n.bubble = el("div", { class: "bubble-row" }, [el("div", { class: "bubble now", text: question }), n.rewrite]);
    n.status = el("span", { class: "status-line", text: "Running checklist" });
    n.elapsed = el("span", { class: "elapsed", text: "0.00 s" });
    n.fill = el("div", { class: "track-fill" });
    n.drone = el("div", { class: "flight-drone" }, [icon(ICONS.drone)]);
    n.waypoints = el("div");
    n.flight = el("div", { class: "flight", "aria-hidden": "true" }, [el("div", { class: "track" }), n.fill, n.waypoints, n.drone]);
    n.grid = el("ol", { class: "stage-grid", "aria-label": "Preflight stages" });
    n.preflight = el("div", { class: "card preflight" }, [
      el("div", { class: "preflight-head" }, [
        el("div", { class: "preflight-title" }, [el("span", { class: "label-mono", text: "PREFLIGHT" }), n.status]),
        n.elapsed,
      ]),
      n.flight,
      n.grid,
    ]);
    n.results = el("div", { class: "results" });
    n.followups = el("div", { class: "followups", hidden: true });
    n.root = el("div", { class: "current-turn" }, [n.bubble, n.preflight, n.results, n.followups]);
    turn.timer = setInterval(() => {
      n.elapsed.textContent = seconds(performance.now() - turn.started);
    }, 100);
    renderStages(turn);
    return turn;
  }

  function visibleStages(turn) {
    // Hide stages skipped after the answer stopped (UI_SPEC 4.4: a gate refusal shows four tiles).
    const stoppedAt = STAGES.findIndex((s) => turn.stages[s].status === "stopped");
    return STAGES.filter((s, i) => !(stoppedAt >= 0 && i > stoppedAt && turn.stages[s].status !== "done"));
  }

  function stageLabel(turn, name) {
    const a = turn.answer;
    if (name === "generate" && a && a.trace && /^evidence gate/.test(a.trace.refusal_reason || "")) {
      return "Evidence gate: not enough support";
    }
    if (name === "check" && a && a.trace && a.trace.checks && Array.isArray(a.trace.checks.requested)) {
      const n = a.trace.checks.requested.length;
      if (n) return "Checking " + n + " safeguard" + (n === 1 ? "" : "s");
    }
    return STAGE_LABELS[name];
  }

  function renderStages(turn) {
    const n = turn.nodes;
    const names = visibleStages(turn);
    n.grid.replaceChildren();
    n.waypoints.replaceChildren();
    let reached = 0;
    let stopped = false;
    names.forEach((name, i) => {
      const s = turn.stages[name];
      const status = s.status; // pending | active | done | stopped | skipped
      if (status === "done" || status === "stopped" || status === "skipped") reached = i + 1;
      if (status === "stopped") stopped = true;
      const iconKey = status === "done" ? "done" : status === "active" ? "active" : status === "stopped" ? "stopped" : "pending";
      const msLabel = status === "done" || status === "stopped" ? Math.round(s.ms) + " ms"
        : status === "active" ? "in progress" : status === "skipped" ? "not used" : "waiting";
      n.grid.appendChild(el("li", { class: "stage " + status }, [
        el("span", { class: "stage-icon" }, [icon(ICONS[iconKey])]),
        el("span", { class: "stage-text" }, [
          el("span", { class: "stage-name", text: stageLabel(turn, name) }),
          el("span", { class: "stage-ms", text: msLabel }),
        ]),
      ]));
      const at = ((i + 1) / names.length) * 100;
      const cls = status === "stopped" ? "stopped" : status === "done" || status === "skipped" ? "reached" : status === "active" ? "current" : "";
      const wp = el("div", { class: "waypoint " + cls });
      wp.style.left = "calc(" + at + "% - 6px)";
      n.waypoints.appendChild(wp);
    });
    const pct = names.length ? (reached / names.length) * 100 : 0;
    n.fill.style.width = pct + "%";
    n.fill.classList.toggle("stopped", stopped);
    n.drone.style.left = "calc(" + pct + "% - 20px)";
  }

  function onStage(turn, data) {
    if (data.config && data.config !== "b3") return; // the preflight card follows the main answer
    const s = turn.stages[data.name];
    if (!s) return;
    s.status = data.status === "started" ? "active" : data.status;
    if (typeof data.ms === "number") s.ms = data.ms;
    renderStages(turn);
  }

  function finishPreflight(turn) {
    clearInterval(turn.timer);
    const a = turn.answer;
    const n = turn.nodes;
    if (a && a.latency_ms && typeof a.latency_ms.total === "number") n.elapsed.textContent = seconds(a.latency_ms.total);
    n.status.classList.remove("good", "warn");
    if (turn.error || !a) {
      n.status.textContent = "Not cleared: something went wrong";
      n.status.classList.add("warn");
      for (const s of STAGES) if (turn.stages[s].status === "active") turn.stages[s].status = "stopped";
      renderStages(turn);
    } else if (a.status === "answered") {
      n.status.textContent = "Cleared: answer verified";
      n.status.classList.add("good");
    } else {
      n.status.textContent = "Not cleared: not enough support in the sources";
      n.status.classList.add("warn");
    }
    announce(n.status.textContent);
  }

  // --- Answer card ----------------------------------------------------------------------------
  function passageText(text) {
    // Structure-aware chunks start with their heading path, then a blank line.
    const parts = (text || "").split(/\n\s*\n/);
    return parts.length > 1 && parts[0].includes(" > ") ? parts.slice(1).join(" ") : text || "";
  }

  function headingOf(text) {
    const first = (text || "").split("\n")[0];
    return first.includes(" > ") ? first.split(" > ").pop().trim() : null;
  }

  const STOP = new Set("a an and are as at be by can for from has have if in is it may must not of on or that the their them they this to under was what when where which who will with you your".split(" "));
  function words(s) {
    return (s || "").toLowerCase().match(/[a-z0-9.]+/g) || [];
  }

  // The best window of at most 25 words of the passage, with the words it shares with the sentence marked.
  function excerpt(sentence, passage) {
    const target = new Set(words(sentence).filter((w) => !STOP.has(w)));
    const tokens = passageText(passage).split(/\s+/).filter(Boolean);
    const size = Math.min(25, tokens.length);
    let best = 0;
    let bestScore = -1;
    for (let i = 0; i + size <= tokens.length; i++) {
      let score = 0;
      for (let j = i; j < i + size; j++) if (target.has(words(tokens[j])[0])) score++;
      if (score > bestScore) { bestScore = score; best = i; }
    }
    const span = el("span");
    span.appendChild(document.createTextNode(best > 0 ? "“..." : "“"));
    tokens.slice(best, best + size).forEach((tok, k) => {
      if (k) span.appendChild(document.createTextNode(" "));
      const hit = target.has(words(tok)[0]);
      span.appendChild(hit ? el("mark", { text: tok }) : document.createTextNode(tok));
    });
    span.appendChild(document.createTextNode(best + size < tokens.length ? "...”" : "”"));
    return span;
  }

  function shortPath(url) {
    try {
      const u = new URL(url);
      const path = u.pathname.length > 48 ? "..." + u.pathname.slice(-45) : u.pathname;
      return u.hostname.replace(/^www\./, "") + path;
    } catch (e) {
      return url || "";
    }
  }

  function citeButton(turn, citation, sentenceText, passage) {
    const tip = el("span", { class: "tip", role: "tooltip", hidden: true }, [
      el("span", { class: "tip-label", text: "PASSAGE FROM SOURCE " + citation.n }),
      passage ? excerpt(sentenceText, passage.text) : el("span", { text: "Passage not available." }),
      el("span", { class: "tip-source", text: citation.title + " · " + shortPath(citation.url) }),
    ]);
    const show = () => { tip.hidden = false; };
    const hide = () => { tip.hidden = true; };
    const btn = el("button", {
      type: "button",
      class: "cite",
      text: String(citation.n),
      "aria-label": "Source " + citation.n + ": " + citation.title,
      onmouseenter: show,
      onmouseleave: hide,
      onfocus: show,
      onblur: hide,
      onkeydown: (e) => { if (e.key === "Escape") hide(); },
      onclick: () => selectSource(citation.n),
    });
    btn.dataset.n = citation.n;
    return el("span", { class: "cite-wrap" }, [btn, tip]);
  }

  function keyFigure(a) {
    const checks = (a.trace && a.trace.checks) || {};
    if (!checks.per_check || checks.per_check.number_date !== true) return null; // verified numbers only
    const first = a.sentences[0];
    const m = first && first.text.match(FIGURE_UNITS);
    if (!m) return null;
    const unit = { foot: "FT", feet: "FT", ft: "FT", pound: "LB", pounds: "LB", lb: "LB", lbs: "LB", gram: "G", grams: "G", g: "G" }[m[2].toLowerCase()] || m[2].toUpperCase();
    const n = (a.citations[0] || {}).n;
    return el("div", { class: "figure sr-fade" }, [
      el("div", { class: "figure-readout" }, [
        el("span", { class: "figure-value", text: m[1] }),
        el("span", { class: "figure-unit", text: unit }),
      ]),
      el("div", { class: "figure-text" }, [
        el("span", { class: "figure-label", text: "KEY FIGURE" }),
        el("span", { class: "figure-caption", text: "From the first sentence, checked against source " + (n || 1) }),
      ]),
    ]);
  }

  function referralPanel(referral) {
    if (!referral) return null;
    const location = referral.kind === "location";
    const links = location
      ? [
          el("a", { class: "btn-solid", href: B4UFLY_URL, target: "_blank", rel: "noopener noreferrer", text: "Open B4UFLY" }),
          el("a", { class: "btn-outline", href: FACILITY_MAPS_URL, target: "_blank", rel: "noopener noreferrer", text: "UAS Facility Maps" }),
        ]
      : [el("a", { class: "btn-solid", href: FAA_UAS_URL, target: "_blank", rel: "noopener noreferrer", text: "FAA drone information" })];
    return el("div", { class: "referral sr-fade" }, [
      el("div", { class: "referral-head" }, [
        icon(ICONS.referral),
        el("span", { text: location ? "Check your exact location before you fly" : "Confirm with the FAA before you fly" }),
      ]),
      el("p", { text: referral.text }),
      el("div", { class: "referral-links" }, links),
    ]);
  }

  function checksRow(a) {
    const checks = (a.trace && a.trace.checks) || {};
    const requested = checks.requested || [];
    const per = checks.per_check || {};
    const passed = requested.filter((c) => per[c] === true).length;
    const items = requested.map((c) =>
      el("span", { class: "check-item" }, [(per[c] ? icon(ICONS.check) : icon(ICONS.cross)), CHECK_LABELS[c] || c]),
    );
    const head = requested.length
      ? el("span", { class: "cleared", text: "CLEARED " + passed + "/" + requested.length })
      : el("span", { class: "cleared", text: "NO ANSWER CHECKS IN THIS CONFIG" });
    return el("div", { class: "checks-row sr-fade" }, [
      el("div", { class: "checks-list" }, [head].concat(items)),
      el("span", { class: "answered-in", text: "Answered in " + seconds(a.latency_ms && a.latency_ms.total) }),
    ]);
  }

  // The router tag ("Location-specific" and so on), shown on answer and refusal cards alike.
  function tagPill(a) {
    return a.router_tag && a.router_tag !== "normal"
      ? el("span", { class: "pill tag", text: TAG_LABELS[a.router_tag] || a.router_tag })
      : null;
  }

  function answerCard(turn, a) {
    const byId = Object.fromEntries((a.trace && a.trace.reranked || []).map((p) => [p.chunk_id, p]));
    const citeById = Object.fromEntries(a.citations.map((c) => [c.chunk_id, c]));
    const head = el("div", { class: "answer-head" }, [
      el("span", { class: "answer-title", text: "Verified answer" }),
      el("span", { class: "pill pilot", text: "For: " + (PILOT_NAMES[turn.pilot] || PILOT_NAMES.not_sure) }),
      tagPill(a),
    ]);
    const list = el("div", { class: "sentences" });
    const card = el("article", { class: "card answer sr-fade", "aria-label": "Verified answer" }, [head, keyFigure(a), list]);
    const tail = [referralPanel(a.referral), checksRow(a)].filter(Boolean);

    const sentenceNodes = a.sentences.map((s) => {
      const p = el("p", { class: "sentence sr-fade", text: s.text });
      for (const id of s.chunk_ids) {
        const c = citeById[id];
        if (c) p.appendChild(citeButton(turn, c, s.text, byId[id]));
      }
      return p;
    });
    const steps = sentenceNodes.concat(tail);
    if (reduceMotion) {
      for (const node of sentenceNodes) list.appendChild(node);
      for (const node of tail) card.appendChild(node);
    } else {
      steps.forEach((node, i) => {
        turn.revealTimers.push(setTimeout(() => {
          (i < sentenceNodes.length ? list : card).appendChild(node);
        }, i * REVEAL_MS));
      });
    }
    return card;
  }

  const REASONS = [
    [/^evidence gate/, "Evidence gate: no passage scored above the threshold"],
    [/^not enough support/, "Not enough support in the passages"],
    [/^check failed: (.*)/, (m) => "Failed check: " + m[1]],
    [/^check error/, "A safeguard check could not run, so SkyRules did not answer"],
    [/^generation error/, "The answer could not be written in a checkable form"],
    [/^nothing left to answer/, "Only instructions were found in the question, and they were removed"],
  ];
  function reasonText(reason) {
    for (const [rx, out] of REASONS) {
      const m = (reason || "").match(rx);
      if (m) return typeof out === "function" ? out(m) : out;
    }
    return reason || "Not enough support in the sources";
  }

  function refusalCard(a) {
    const tag = tagPill(a);
    return el("article", { class: "refusal sr-fade", "aria-label": "Refusal" }, [
      tag ? el("div", { class: "answer-head" }, [tag]) : null,
      el("div", { class: "refusal-head" }, [icon(ICONS.refusal), el("span", { class: "refusal-title", text: REFUSAL_TEXT })]),
      el("p", { text: REFUSAL_HINT }),
      el("div", { class: "refusal-foot" }, [
        el("span", { text: reasonText(a.trace && a.trace.refusal_reason) }),
        el("span", { class: "mono", text: seconds(a.latency_ms && a.latency_ms.total) }),
      ]),
    ]);
  }

  function errorCard(message) {
    return el("article", { class: "refusal sr-fade", "aria-label": "Error" }, [
      el("div", { class: "refusal-head" }, [icon(ICONS.refusal), el("span", { class: "refusal-title", text: ERROR_TEXT })]),
      message ? el("div", { class: "refusal-foot" }, [el("span", { text: "Details: " + message })]) : null,
    ]);
  }

  function b2Card(b2) {
    const flags = Array.isArray(b2.b3_check_flags) ? b2.b3_check_flags : null;
    const body = [
      el("div", { class: "b2-title" }, [
        el("span", { class: "b2-name", text: "B2 BASELINE" }),
        el("span", { class: "b2-desc", text: "Dense retrieval, fixed chunks, no reranker, no answer checks" }),
      ]),
    ];
    if (b2.status !== "answered") {
      body.push(el("p", { class: "b2-note", text: "B2 also refused: " + reasonText(b2.trace && b2.trace.refusal_reason) }));
    } else {
      b2.sentences.forEach((s, i) => {
        const failed = flags ? flags[i] || [] : null;
        const marks = failed === null
          ? [el("span", { class: "b2-note", text: "B3 checks could not run on this sentence" })]
          : failed.length
            ? failed.map((f) => el("span", { class: "flag" }, [icon(ICONS.warn), f]))
            : [el("span", { class: "pass", text: "Passes the B3 checks" })];
        body.push(el("div", { class: "b2-sentence" }, [el("p", { text: s.text })].concat(marks)));
      });
    }
    return el("article", { class: "b2-card sr-fade", "aria-label": "B2 baseline answer" }, body);
  }

  // The B3 column is built once (so its reveal is not restarted); a later B2 answer is added beside it.
  function renderResults(turn) {
    const n = turn.nodes;
    if (!n.b3col && (turn.answer || turn.error)) {
      const b3 = el("div", { class: "b3-col" });
      if (turn.compare) b3.appendChild(el("span", { class: "b3-label", text: "B3 FINAL SYSTEM" }));
      if (turn.answer && turn.answer.status === "answered") {
        b3.appendChild(answerCard(turn, turn.answer));
      } else if (turn.answer) {
        b3.appendChild(refusalCard(turn.answer));
        const r = referralPanel(turn.answer.referral);
        if (r) b3.appendChild(r);
      } else {
        b3.appendChild(errorCard(turn.error));
      }
      n.b3col = b3;
      n.results.appendChild(b3);
    }
    if (turn.b2 && !n.b2card) {
      n.b2card = b2Card(turn.b2);
      n.results.insertBefore(n.b2card, n.results.firstChild);
    }
  }

  // --- Follow-up chips (no extra LLM call) ----------------------------------------------------
  const FIXED_FOLLOWUPS = {
    answered: ["Do I need to register my drone?", "Can I fly my drone at night?"],
    refused: ["How high can I fly as a hobby flyer?", "Do I need to register my drone?"],
    location_live: ["How high can I fly as a hobby flyer?", "Can I fly my drone at night?"],
  };
  function followups(turn) {
    const a = turn.answer;
    if (!a) return [];
    const out = [];
    if (a.status === "answered") {
      for (const p of (a.trace && a.trace.reranked) || []) {
        const h = headingOf(p.text);
        if (h && h.length < 70 && !turn.question.toLowerCase().includes(h.toLowerCase())) {
          const q = "Tell me more about " + h.replace(/[.?!]+$/, "");
          if (!out.includes(q)) out.push(q);
        }
        if (out.length === 2) return out;
      }
    }
    const fixed = FIXED_FOLLOWUPS[a.router_tag] || FIXED_FOLLOWUPS[a.status] || FIXED_FOLLOWUPS.answered;
    for (const q of fixed) if (out.length < 2 && q !== turn.question && !out.includes(q)) out.push(q);
    return out;
  }

  function renderFollowups(turn) {
    const qs = followups(turn);
    const box = turn.nodes.followups;
    box.replaceChildren(el("span", { class: "followups-label", text: "Ask next" }));
    for (const q of qs) box.appendChild(el("button", { type: "button", class: "followup-chip", text: q, onclick: () => ask(q) }));
    box.hidden = qs.length === 0;
  }

  // --- Side panel: sources and trace ------------------------------------------------------------
  function freshness(iso) {
    if (!iso || isNaN(Date.parse(iso))) return { cls: "unknown", label: "Date not found in the source" };
    const years = (Date.now() - Date.parse(iso)) / (365.25 * 864e5);
    if (years < 1) return { cls: "new", label: "Updated within a year" };
    if (years < 2) return { cls: "year", label: "Over a year old" };
    return { cls: "old", label: "Over 2 years old, check for changes" };
  }

  function formatDate(iso) {
    if (!iso || isNaN(Date.parse(iso))) return "not found";
    return new Date(iso + (iso.length === 10 ? "T00:00:00" : "")).toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric" });
  }

  function renderSources(answer) {
    const list = $("source-list");
    list.replaceChildren();
    const cites = answer && answer.status === "answered" ? answer.citations : [];
    const types = Object.fromEntries(((answer && answer.trace && answer.trace.reranked) || []).map((p) => [p.chunk_id, p.page_type]));
    $("sources-empty").hidden = cites.length > 0;
    $("source-count").textContent = cites.length ? cites.length + (cites.length === 1 ? " source" : " sources") : "";
    for (const c of cites) {
      const f = freshness(c.effective_date);
      const url = safeUrl(c.url);
      list.appendChild(el("div", { class: "source", id: "source-" + c.n }, [
        el("div", { class: "source-meta" }, [
          el("span", { class: "source-n", text: "[" + c.n + "]" }),
          types[c.chunk_id] ? el("span", { class: "badge", text: PAGE_TYPES[types[c.chunk_id]] || types[c.chunk_id] }) : null,
          el("span", { class: "fresh " + f.cls }, [el("span", { class: "dot" }), f.label]),
        ]),
        url
          ? el("a", { class: "source-title", href: url, target: "_blank", rel: "noopener noreferrer", text: c.title })
          : el("span", { class: "source-title", text: c.title }),
        c.source_name ? el("span", { class: "source-name", text: c.source_name }) : null,
        el("span", { class: "source-path", text: shortPath(c.url) }),
        el("span", { class: "source-date", text: "Last updated: " + formatDate(c.effective_date) }),
      ]));
    }
  }

  function selectSource(n) {
    for (const node of document.querySelectorAll(".source")) node.classList.toggle("on", node.id === "source-" + n);
    for (const b of document.querySelectorAll(".cite")) b.classList.toggle("on", b.dataset.n === String(n));
    const target = $("source-" + n);
    if (target) target.scrollIntoView({ block: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
  }

  function traceLabel(c) {
    const h = headingOf(c.text);
    return (c.title || c.chunk_id) + (h ? " › " + h : "");
  }

  function fmt(x, digits) {
    return typeof x === "number" ? x.toFixed(digits) : "n/a";
  }

  function renderTrace(answer) {
    const body = $("trace-body");
    body.replaceChildren();
    const toggle = $("trace-toggle");
    $("trace-toggle-label").textContent = !answer ? "Ask a question first" : state.traceOpen ? "Hide" : "Show";
    toggle.setAttribute("aria-expanded", state.traceOpen ? "true" : "false");
    body.hidden = !(answer && state.traceOpen);
    if (!answer) return;
    const trace = answer.trace || {};
    const candidates = trace.candidates || [];
    const reranked = trace.reranked || [];
    const rerankScore = Object.fromEntries(reranked.map((p) => [p.chunk_id, p.rerank_score]));
    const scores = reranked.map((p) => p.rerank_score).filter((s) => typeof s === "number");
    const lo = Math.min(...scores);
    const hi = Math.max(...scores);
    const norm = (s) => (typeof s !== "number" ? 0 : hi > lo ? (s - lo) / (hi - lo) : 1);
    if (!candidates.length) {
      body.appendChild(el("p", { class: "trace-note", text: "No passages were retrieved for this question." }));
      return;
    }
    body.appendChild(el("p", {
      class: "trace-note",
      text: "All " + candidates.length + " candidates. Highlighted rows passed reranking into the top " + reranked.length + ".",
    }));
    const rows = el("div", { class: "trace-rows" });
    for (const c of candidates) {
      const kept = c.chunk_id in rerankScore;
      const dense = el("div", { class: "bar" });
      dense.style.width = Math.round(Math.max(0, Math.min(1, c.dense_score || 0)) * 100) + "%";
      const rer = el("div", { class: "bar rerank" });
      rer.style.width = Math.round(norm(rerankScore[c.chunk_id]) * 100) + "%";
      rows.appendChild(el("div", { class: "trace-row" + (kept ? " kept" : "") }, [
        el("div", { class: "trace-top" }, [
          el("span", { class: "trace-label", text: traceLabel(c) }),
          el("span", { class: "trace-score", text: kept ? fmt(rerankScore[c.chunk_id], 2) : "not reranked" }),
        ]),
        el("div", { class: "bar-line" }, [el("span", { class: "bar-name", text: "dense" }), el("div", { class: "bar-track" }, [dense])]),
        el("div", { class: "bar-line" }, [el("span", { class: "bar-name", text: "rerank" }), el("div", { class: "bar-track" }, [rer])]),
        el("span", { class: "trace-detail", text: "bm25 " + fmt(c.bm25_score, 2) + " · fused " + fmt(c.fused_score, 4) }),
      ]));
    }
    body.appendChild(rows);
  }

  // --- Conversation ------------------------------------------------------------------------------
  function summaryOf(turn) {
    const a = turn.answer;
    if (turn.error || !a) return "Something went wrong, so SkyRules did not answer.";
    if (a.status !== "answered") return "Refused: not covered by the sources SkyRules searches.";
    const checks = (a.trace && a.trace.checks) || {};
    const req = (checks.requested || []).length;
    const ok = Object.values(checks.per_check || {}).filter(Boolean).length;
    const nSrc = a.citations.length;
    return "Verified answer, " + nSrc + (nSrc === 1 ? " source" : " sources") + (req ? ", " + ok + " of " + req + " checks passed." : ".");
  }

  function historyMessages() {
    const msgs = [];
    for (const t of state.turns.slice(-MAX_TURNS)) {
      msgs.push({ role: "user", content: t.question });
      msgs.push({ role: "assistant", content: t.assistant });
    }
    return msgs;
  }

  function renderThread() {
    const thread = $("thread");
    thread.replaceChildren();
    const hasAny = state.turns.length > 0 || state.current;
    $("empty-state").hidden = !!hasAny;
    thread.hidden = !hasAny;
    if (!hasAny) return;
    thread.appendChild(el("div", { class: "turn-bar" }, [
      el("span", { class: "turn-label", text: "Turn " + (state.turns.length + (state.current ? 1 : 0)) + " · chat history is used to understand follow-ups" }),
      el("button", { type: "button", class: "plain-btn", text: "New chat", onclick: newChat }),
    ]));
    for (const t of state.turns) {
      thread.appendChild(el("div", { class: "old-turn" }, [
        el("div", { class: "bubble-row" }, [el("div", { class: "bubble old", text: t.question })]),
        el("div", { class: "summary", text: t.summary }),
      ]));
    }
    if (state.current) thread.appendChild(state.current.nodes.root);
  }

  function archiveCurrent() {
    const t = state.current;
    if (!t || !t.finished) return;
    t.revealTimers.forEach(clearTimeout);
    const a = t.answer;
    const assistant = a && a.status === "answered"
      ? a.sentences.map((s) => s.text).join(" ")
      : summaryOf(t);
    state.turns.push({ question: t.question, assistant, summary: summaryOf(t) });
    if (state.turns.length > MAX_TURNS) state.turns = state.turns.slice(-MAX_TURNS);
    state.current = null;
  }

  function newChat() {
    if (state.busy) return;
    if (state.current) state.current.revealTimers.forEach(clearTimeout);
    state.turns = [];
    state.current = null;
    renderThread();
    renderSources(null);
    renderTrace(null);
    $("ask-input").focus();
  }

  function setBusy(busy) {
    state.busy = busy;
    $("ask-btn").disabled = busy;
    $("ask-btn").textContent = busy ? "Asking..." : "Ask";
  }

  async function ask(question) {
    question = (question || "").trim();
    if (!question || state.busy) return;
    archiveCurrent();
    const history = historyMessages();
    const turn = newTurn(question);
    turn.pilot = state.pilot;
    turn.compare = state.compare;
    state.current = turn;
    $("ask-input").value = "";
    setBusy(true);
    renderThread();
    renderSources(null);
    renderTrace(null);
    announce("Running checklist");
    turn.nodes.root.scrollIntoView({ block: "start", behavior: reduceMotion ? "auto" : "smooth" });

    try {
      await streamAsk({ question, history, pilot_type: state.pilot, compare: state.compare }, (ev) => {
        if (ev.event === "stage") onStage(turn, ev.data);
        else if (ev.event === "answer") {
          if (ev.data.config === "b2") turn.b2 = ev.data;
          else turn.answer = ev.data;
          if (turn.answer && turn.answer.rewritten_question) {
            turn.nodes.rewrite.textContent = "Rewritten with chat history: " + turn.answer.rewritten_question;
            turn.nodes.rewrite.hidden = false;
          }
          if (ev.data.config !== "b2") {
            finishPreflight(turn);
            renderSources(turn.answer);
            renderTrace(turn.answer);
          }
          renderResults(turn);
        } else if (ev.event === "error") {
          turn.error = ev.data.message || "unknown error";
          if (!turn.answer) {
            finishPreflight(turn);
            renderResults(turn);
          }
        }
      });
    } catch (e) {
      turn.error = e && e.message ? e.message : String(e);
    }
    if (!turn.answer) {
      if (!turn.error) turn.error = "the stream ended without an answer";
      finishPreflight(turn);
      renderResults(turn);
    }
    turn.finished = true;
    renderFollowups(turn);
    setBusy(false);
  }

  // --- Wire up -----------------------------------------------------------------------------------
  function init() {
    setTheme(storeGet("skyrules-theme") === "dark");
    $("mock-tag").hidden = !state.mock;
    for (const b of document.querySelectorAll(".pilot-btn")) b.addEventListener("click", () => setPilot(b.dataset.pilot));
    $("compare-btn").addEventListener("click", () => setCompare(!state.compare));
    $("theme-btn").addEventListener("click", () => setTheme(document.documentElement.getAttribute("data-theme") !== "dark"));
    for (const chip of document.querySelectorAll(".example-chip")) chip.addEventListener("click", () => ask(chip.textContent));
    $("ask-form").addEventListener("submit", (e) => {
      e.preventDefault();
      ask($("ask-input").value);
    });
    $("trace-toggle").addEventListener("click", () => {
      state.traceOpen = !state.traceOpen;
      renderTrace(state.current && state.current.answer);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") for (const t of document.querySelectorAll(".tip")) t.hidden = true;
    });
  }

  init();
})();
