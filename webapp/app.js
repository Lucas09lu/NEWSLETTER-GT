/* Global Transport · Inteligencia Comex — lector PWA.
 * Lee data/index.json (lista de ediciones) y data/edition_YYYY-MM-DD.json.
 * Sin dependencias ni build: HTML + JS + CSS estáticos (GitHub Pages).
 * Todo el contenido se inserta con textContent para evitar inyección de HTML.
 */
(function () {
  "use strict";

  var PILLARS = [
    { id: "logistica", short: "Logística y fletes", label: "Logística Global, Fletes y Puertos" },
    { id: "comex", short: "Comex y cifras", label: "Comercio Exterior Peruano y Cifras Macro" },
    { id: "regulacion", short: "Regulación y TLC", label: "Regulación, Aduanas y Acuerdos Comerciales" },
    { id: "sectorial", short: "Agro, minería, industria", label: "Pulso Sectorial: Agro, Minería e Industria" },
    { id: "global", short: "Contexto global", label: "Contexto Global y Tendencias" }
  ];
  var MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "set", "oct", "nov", "dic"];

  var state = { index: [], edition: null, pillar: "all", allOpen: false };
  var $ = function (id) { return document.getElementById(id); };

  /* ---------- utilidades DOM ---------- */
  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") el.textContent = attrs[k];
      else if (k === "className") el.className = attrs[k];
      else if (attrs[k] !== null && attrs[k] !== undefined) el.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) {
      if (c === null || c === undefined) return;
      el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return el;
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
  function parseDate(iso) { var p = iso.slice(0, 10).split("-"); return new Date(+p[0], +p[1] - 1, +p[2]); }
  function fmtDate(iso) { var d = parseDate(iso); return d.getDate() + " " + MONTHS[d.getMonth()] + " " + d.getFullYear(); }
  function rangeLabel(win) {
    var s = parseDate(win.start), e = parseDate(win.end);
    return s.getMonth() === e.getMonth()
      ? s.getDate() + "–" + e.getDate() + " " + MONTHS[e.getMonth()] + " " + e.getFullYear()
      : s.getDate() + " " + MONTHS[s.getMonth()] + "–" + e.getDate() + " " + MONTHS[e.getMonth()] + " " + e.getFullYear();
  }
  function safeUrl(url) { return /^https?:\/\//i.test(url || "") ? url : "#"; }
  function pillarInfo(id) { return PILLARS.filter(function (p) { return p.id === id; })[0] || PILLARS[0]; }

  /* ---------- datos ---------- */
  function fetchJson(url) {
    return fetch(url, { cache: "no-cache" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status + " en " + url);
      return r.json();
    });
  }

  function currentDateFromHash() {
    var m = location.hash.match(/#\/(\d{4}-\d{2}-\d{2})/);
    return m ? m[1] : null;
  }

  function loadEdition(date) {
    return fetchJson("data/edition_" + date + ".json").then(function (ed) {
      state.edition = ed;
      state.pillar = "all";
      state.allOpen = false;
      render();
      window.scrollTo(0, 0);
    });
  }

  /* ---------- render ---------- */
  function render() {
    var ed = state.edition;
    if (!ed) return;
    $("edition-range").textContent = rangeLabel(ed.window);
    $("edition-theme").textContent = "Edición del " + fmtDate(ed.edition_date) + " · " + ed.main_theme;
    document.title = "Comex GT · " + rangeLabel(ed.window);
    $("sample-banner").hidden = !ed.sample;

    var notice = $("notice");
    if (ed.curation_mode !== "claude" && !ed.sample) {
      notice.textContent = "Edición en modo automático básico: el análisis de impacto se activa al configurar la API de Claude.";
      notice.hidden = false;
    } else {
      notice.hidden = true;
    }

    renderSelect();
    renderChips();
    renderSummary();
    renderStories();
    renderRadar();
    renderArchive();

    var st = ed.stats || {};
    $("footer-stats").textContent =
      "Fecha de corte: " + fmtDate(ed.window.end) + ", 23:59 (hora Lima) · " +
      (st.sources_total || 0) + " fuentes procesadas.";
  }

  function renderSelect() {
    var sel = $("edition-select");
    clear(sel);
    state.index.forEach(function (e) {
      var opt = h("option", { value: e.date, text: "Edición " + fmtDate(e.date) + (e.sample ? " (muestra)" : "") });
      if (state.edition && e.date === state.edition.edition_date) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  function renderChips() {
    var nav = $("pillar-chips");
    clear(nav);
    var stories = state.edition.stories;
    var make = function (id, label, count) {
      var b = h("button", { type: "button", className: "chip", "aria-pressed": String(state.pillar === id) },
        [label, h("span", { className: "chip__count", text: String(count) })]);
      b.addEventListener("click", function () { state.pillar = id; renderChips(); renderStories(); });
      return b;
    };
    nav.appendChild(make("all", "Todo", stories.length));
    PILLARS.forEach(function (p) {
      var n = stories.filter(function (s) { return s.pillar === p.id; }).length;
      if (n) nav.appendChild(make(p.id, p.short, n));
    });
  }

  function renderSummary() {
    var list = $("summary-list");
    clear(list);
    var items = state.edition.executive_summary || [];
    items.forEach(function (b) { list.appendChild(h("li", { text: b })); });
    $("summary").hidden = !items.length || state.pillar !== "all";
  }

  function storyCard(s) {
    var p = pillarInfo(s.pillar);
    var card = h("article", { className: "card story", "data-open": String(state.allOpen) });
    var bodyId = "body-" + s.id;
    var head = h("button", { type: "button", className: "story__head", "aria-expanded": String(state.allOpen), "aria-controls": bodyId }, [
      h("span", { className: "story__chev", "aria-hidden": "true", text: "▾" }),
      h("span", { className: "story__tag", text: p.short }),
      s.is_official ? h("span", { className: "story__official", text: "Fuente oficial" }) : null,
      h("h3", { className: "story__title", text: s.headline }),
      h("p", { className: "story__impact-preview" }, [h("b", { text: "Impacto: " }), s.impact])
    ]);
    head.addEventListener("click", function () {
      var open = card.getAttribute("data-open") !== "true";
      card.setAttribute("data-open", String(open));
      head.setAttribute("aria-expanded", String(open));
    });

    var sourceLine = h("p", { className: "story__source" }, [
      "Fuente: ",
      h("a", { href: safeUrl(s.source.url), target: "_blank", rel: "noopener", text: s.source.name }),
      s.paywalled ? " (resumen público)" : null
    ]);
    (s.also_reported_by || []).forEach(function (ref, i) {
      sourceLine.appendChild(document.createTextNode(i === 0 ? " · También: " : ", "));
      sourceLine.appendChild(h("a", { href: safeUrl(ref.url), target: "_blank", rel: "noopener", text: ref.name }));
    });

    var body = h("div", { className: "story__body", id: bodyId }, [
      h("p", { className: "story__what", text: s.what_happened }),
      h("p", { className: "story__impact" }, [h("b", { text: "Impacto comercial: " }), s.impact]),
      sourceLine
    ]);
    card.appendChild(head);
    card.appendChild(body);
    return card;
  }

  function renderStories() {
    var wrap = $("stories");
    clear(wrap);
    var stories = state.edition.stories.filter(function (s) {
      return state.pillar === "all" || s.pillar === state.pillar;
    });
    $("story-count").textContent = stories.length + (stories.length === 1 ? " noticia" : " noticias");
    $("toggle-all").textContent = state.allOpen ? "Contraer todo" : "Expandir todo";
    renderSummary();
    if (!stories.length) {
      wrap.appendChild(h("p", { className: "empty", text: "Sin novedades relevantes en este pilar esta semana." }));
      return;
    }
    PILLARS.forEach(function (p) {
      var group = stories.filter(function (s) { return s.pillar === p.id; });
      if (!group.length) return;
      if (state.pillar === "all") wrap.appendChild(h("h2", { className: "pillar-heading", text: p.label }));
      group.forEach(function (s) { wrap.appendChild(storyCard(s)); });
    });
  }

  function renderRadar() {
    var radar = state.edition.radar || [];
    var list = $("radar-list");
    clear(list);
    radar.forEach(function (r) {
      list.appendChild(h("div", { className: "radar-row" }, [
        h("a", { className: "radar-row__label", href: safeUrl(r.source.url), target: "_blank", rel: "noopener", style: "color:inherit;text-decoration:none", text: r.label }),
        h("span", { className: "radar-row__value", text: r.value }),
        h("span", { className: "radar-row__meta", text: r.period + " · " + r.source.name }),
        h("span", { className: "radar-row__change" + (r.trend === "down" ? " down" : ""),
          text: (r.trend === "up" ? "▲ " : r.trend === "down" ? "▼ " : "") + (r.change || "") })
      ]));
    });
    $("radar").hidden = !radar.length;
  }

  function renderArchive() {
    var list = $("archive-list");
    clear(list);
    state.index.forEach(function (e) {
      var current = state.edition && e.date === state.edition.edition_date;
      list.appendChild(h("li", {}, [
        h("a", { href: "#/" + e.date, "aria-current": current ? "page" : null }, [
          h("span", { className: "archive__date", text: fmtDate(e.date) }),
          h("span", { className: "archive__theme", text: (e.sample ? "Muestra · " : "") + e.main_theme })
        ])
      ]));
    });
    $("archive").hidden = state.index.length < 2;
  }

  function showError(msg) {
    var el = $("error");
    el.textContent = msg;
    el.hidden = false;
  }

  /* ---------- instalación ---------- */
  var deferredPrompt = null;
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferredPrompt = e;
    $("install-btn").hidden = false;
  });
  $("install-btn").addEventListener("click", function () {
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    deferredPrompt.userChoice.finally(function () { deferredPrompt = null; $("install-btn").hidden = true; });
  });
  var isIos = /iphone|ipad|ipod/i.test(navigator.userAgent);
  var standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  var iosHintDismissed = false;
  try { iosHintDismissed = localStorage.getItem("gt-ios-hint") === "1"; } catch (e) { /* sin storage */ }
  if (isIos && !standalone && !iosHintDismissed) $("ios-hint").hidden = false;
  document.addEventListener("click", function (e) {
    var id = e.target.getAttribute && e.target.getAttribute("data-close");
    if (!id) return;
    $(id).hidden = true;
    try { localStorage.setItem("gt-ios-hint", "1"); } catch (err) { /* sin storage */ }
  });

  /* ---------- eventos ---------- */
  $("edition-select").addEventListener("change", function (e) { location.hash = "#/" + e.target.value; });
  $("toggle-all").addEventListener("click", function () { state.allOpen = !state.allOpen; renderStories(); });
  window.addEventListener("hashchange", function () {
    var d = currentDateFromHash();
    if (d && (!state.edition || d !== state.edition.edition_date)) loadEdition(d).catch(function (err) { showError("No se pudo cargar la edición " + d + ": " + err.message); });
  });

  /* ---------- arranque ---------- */
  fetchJson("data/index.json")
    .then(function (idx) {
      state.index = (idx.editions || []).slice().sort(function (a, b) { return a.date < b.date ? 1 : -1; });
      if (!state.index.length) throw new Error("Todavía no hay ediciones publicadas.");
      var wanted = currentDateFromHash();
      var exists = state.index.some(function (e) { return e.date === wanted; });
      return loadEdition(exists ? wanted : state.index[0].date);
    })
    .catch(function (err) {
      $("edition-range").textContent = "Inteligencia Comex";
      showError(err.message);
    });

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () { navigator.serviceWorker.register("sw.js").catch(function () {}); });
  }
})();
