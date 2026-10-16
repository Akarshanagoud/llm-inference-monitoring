/* Shared UI kit: DOM helpers, API client, router, tables and charts.
   Plain browser JavaScript with no dependencies, so it is served as-is.
   Every piece of data reaches the DOM through text nodes, never innerHTML. */
(function () {
  "use strict";

  const SVGNS = "http://www.w3.org/2000/svg";

  // ---------------------------------------------------------------- DOM
  function append(el, children) {
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      if (Array.isArray(child)) append(el, child);
      else if (child instanceof Node) el.appendChild(child);
      else el.appendChild(document.createTextNode(String(child)));
    }
    return el;
  }

  function setProps(el, props, isSvg) {
    if (!props) return;
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key.startsWith("on") && typeof value === "function") {
        el.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === "style" && typeof value === "object") {
        Object.assign(el.style, value);
      } else if (key === "class" && !isSvg) {
        el.className = value;
      } else if (key === "dataset") {
        Object.assign(el.dataset, value);
      } else if (value === true) {
        el.setAttribute(key, "");
      } else {
        el.setAttribute(key, String(value));
      }
    }
  }

  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    setProps(el, props, false);
    return append(el, children);
  }

  function s(tag, props, ...children) {
    const el = document.createElementNS(SVGNS, tag);
    setProps(el, props, true);
    return append(el, children);
  }

  function clear(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
    return el;
  }

  // ---------------------------------------------------------------- storage
  const store = {
    get(key, fallback) {
      try {
        const raw = localStorage.getItem(key);
        return raw === null ? fallback : JSON.parse(raw);
      } catch (_) {
        return fallback;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem(key, JSON.stringify(value));
      } catch (_) {
        /* storage unavailable: preferences simply do not persist */
      }
    },
  };

  // ---------------------------------------------------------------- theme
  const THEMES = ["system", "light", "dark"];
  const THEME_ICON = { system: "◐", light: "☀", dark: "☾" };

  function applyTheme(theme) {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
  }

  function initTheme(button) {
    let theme = store.get("kit.theme", "system");
    applyTheme(theme);
    if (!button || button.dataset.kitTheme) return;
    button.dataset.kitTheme = "1";
    const label = () => {
      button.textContent = THEME_ICON[theme];
      button.setAttribute("aria-label", "Theme: " + theme + ". Click to change.");
      button.title = "Theme: " + theme;
    };
    label();
    button.addEventListener("click", () => {
      theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
      store.set("kit.theme", theme);
      applyTheme(theme);
      label();
      window.dispatchEvent(new Event("kit:theme"));
    });
  }

  // ---------------------------------------------------------------- api
  async function request(method, path, body) {
    const init = { method, headers: {} };
    if (body !== undefined) {
      init.headers["content-type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (_) {
      throw new Error("The local server is not reachable. Is it still running?");
    }
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (_) {
      data = text;
    }
    if (!response.ok) {
      let detail = data && data.detail !== undefined ? data.detail : data;
      if (Array.isArray(detail)) detail = detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
      throw new Error(typeof detail === "string" && detail ? detail : response.status + " " + response.statusText);
    }
    return data;
  }

  const api = {
    get: (path) => request("GET", path),
    post: (path, body) => request("POST", path, body === undefined ? {} : body),
    del: (path) => request("DELETE", path),
  };

  // ---------------------------------------------------------------- format
  const compactFmt = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
  const fmt = {
    int(n) {
      return n === null || n === undefined || Number.isNaN(n) ? "—" : Math.round(n).toLocaleString("en-US");
    },
    num(n, digits = 2) {
      if (n === null || n === undefined || Number.isNaN(n)) return "—";
      return Number(n).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
    },
    compact(n) {
      if (n === null || n === undefined || Number.isNaN(n)) return "—";
      return Math.abs(n) < 1000 ? fmt.trim(n) : compactFmt.format(n);
    },
    trim(n) {
      return Number.isInteger(n) ? String(n) : String(Number(Number(n).toFixed(2)));
    },
    pct(x, digits = 1) {
      return x === null || x === undefined || Number.isNaN(x) ? "—" : (x * 100).toFixed(digits) + "%";
    },
    ms(x) {
      if (x === null || x === undefined || Number.isNaN(x)) return "—";
      if (x >= 1000) return (x / 1000).toFixed(2) + " s";
      if (x >= 100) return Math.round(x) + " ms";
      if (x >= 1) return x.toFixed(1) + " ms";
      return x.toFixed(2) + " ms";
    },
    bytes(n) {
      if (n === null || n === undefined) return "—";
      if (n < 1024) return n + " B";
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
      return (n / 1024 / 1024).toFixed(1) + " MB";
    },
    time(value) {
      const d = value instanceof Date ? value : new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
      return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    },
    date(value) {
      const d = value instanceof Date ? value : new Date(value);
      return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleDateString([], { month: "short", day: "numeric" });
    },
    short(hash, n = 8) {
      return hash ? String(hash).slice(0, n) : "—";
    },
    title(text) {
      const t = String(text || "").replace(/_/g, " ");
      return t.charAt(0).toUpperCase() + t.slice(1);
    },
  };

  // ---------------------------------------------------------------- components
  const STATUS_ICON = { good: "✓", warning: "!", serious: "▲", critical: "✕", info: "i", neutral: "•" };

  function card(opts) {
    const { title, subtitle, actions, body, flush, cls, id } = opts;
    const head =
      title || actions
        ? h(
            "div",
            { class: "card-head" },
            h("div", null, title ? h("h2", null, title) : null, subtitle ? h("p", null, subtitle) : null),
            actions ? h("div", { class: "card-actions" }, actions) : null,
          )
        : null;
    return h("section", { class: "card " + (cls || ""), id }, head, h("div", { class: "card-body" + (flush ? " flush" : "") }, body));
  }

  function stat(opts) {
    const { label, value, note, cls, title } = opts;
    return h(
      "div",
      { class: "card stat " + (cls || ""), title },
      h("div", { class: "stat-label" }, label),
      h("div", { class: "stat-value" }, value),
      note ? h("div", { class: "stat-note" }, note) : null,
    );
  }

  function badge(text, kind, opts) {
    const withIcon = opts && opts.icon === false ? false : kind && STATUS_ICON[kind] !== undefined;
    return h(
      "span",
      { class: "badge " + (kind || ""), title: opts && opts.title },
      withIcon ? h("span", { class: "ico", "aria-hidden": "true" }, STATUS_ICON[kind]) : null,
      text,
    );
  }

  function banner(opts) {
    const { kind, title, text, extra } = opts;
    return h(
      "div",
      { class: "banner " + (kind || ""), role: kind === "critical" ? "alert" : "status" },
      h("span", { class: "ico-lg", "aria-hidden": "true" }, STATUS_ICON[kind] || "i"),
      h("div", { style: { minWidth: "0", flex: "1" } }, h("div", { class: "banner-title" }, title), text ? h("div", { class: "ink-2 small" }, text) : null, extra || null),
    );
  }

  function meter(value, kind, label) {
    const pct = Math.max(0, Math.min(1, Number(value) || 0));
    return h(
      "div",
      { class: "meter " + (kind || ""), role: "meter", "aria-valuemin": "0", "aria-valuemax": "1", "aria-valuenow": pct.toFixed(2), "aria-label": label || "" },
      h("i", { style: { width: (pct * 100).toFixed(1) + "%" } }),
    );
  }

  function empty(title, text) {
    return h("div", { class: "empty" }, h("strong", null, title), text ? h("span", null, text) : null);
  }

  function field(label, control) {
    return h("label", { class: "field" }, h("span", null, label), control);
  }

  function select(options, value, onChange, props) {
    const el = h(
      "select",
      Object.assign({ class: "select" }, props || {}),
      options.map((o) => {
        const opt = typeof o === "object" ? o : { value: o, label: o };
        return h("option", { value: opt.value, selected: String(opt.value) === String(value) }, opt.label);
      }),
    );
    if (onChange) el.addEventListener("change", () => onChange(el.value));
    return el;
  }

  function seg(options, value, onChange) {
    const wrap = h("div", { class: "seg", role: "group" });
    const buttons = options.map((o) => {
      const opt = typeof o === "object" ? o : { value: o, label: o };
      const b = h("button", { type: "button", "aria-pressed": String(opt.value === value) }, opt.label);
      b.addEventListener("click", () => {
        buttons.forEach((x) => x.setAttribute("aria-pressed", "false"));
        b.setAttribute("aria-pressed", "true");
        onChange(opt.value);
      });
      return b;
    });
    append(wrap, buttons);
    return wrap;
  }

  function kv(pairs) {
    const dl = h("dl", { class: "kv" });
    for (const [k, v] of pairs) {
      if (v === undefined) continue;
      append(dl, [h("dt", null, k), h("dd", null, v)]);
    }
    return dl;
  }

  function highlight(text, spans) {
    const frag = document.createDocumentFragment();
    const sorted = (spans || [])
      .filter((sp) => sp && sp.end > sp.start)
      .slice()
      .sort((a, b) => a.start - b.start || b.end - a.end);
    let pos = 0;
    for (const sp of sorted) {
      const start = Math.max(sp.start, pos);
      if (start >= sp.end) continue;
      if (start > pos) frag.appendChild(document.createTextNode(text.slice(pos, start)));
      const mark = h("mark", { class: sp.cls || "", title: sp.title || null, tabindex: sp.focusable ? "0" : null }, text.slice(start, sp.end));
      if (sp.id !== undefined) mark.dataset.spanId = sp.id;
      frag.appendChild(mark);
      pos = sp.end;
    }
    if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
    return frag;
  }

  function toast(message, kind) {
    let host = document.getElementById("toasts");
    if (!host) {
      host = h("div", { id: "toasts", class: "toasts", "aria-live": "polite" });
      document.body.appendChild(host);
    }
    const el = h("div", { class: "toast " + (kind || "") }, message);
    host.appendChild(el);
    setTimeout(() => el.remove(), kind === "error" ? 6000 : 3200);
  }

  function download(filename, text, mime) {
    const blob = new Blob([text], { type: mime || "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = h("a", { href: url, download: filename });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function busy(el, fn) {
    el.classList.add("is-busy");
    try {
      return await fn();
    } finally {
      el.classList.remove("is-busy");
    }
  }

  async function withButton(button, fn) {
    const label = button.textContent;
    button.disabled = true;
    try {
      return await fn();
    } catch (err) {
      toast(err.message || String(err), "error");
      return undefined;
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  function errorState(err) {
    return h("div", { class: "card" }, empty("Something went wrong", err && err.message ? err.message : String(err)));
  }

  // ---------------------------------------------------------------- table
  function compare(a, b) {
    if (a === b) return 0;
    if (a === null || a === undefined) return 1;
    if (b === null || b === undefined) return -1;
    if (typeof a === "number" && typeof b === "number") return a - b;
    return String(a).localeCompare(String(b), undefined, { numeric: true });
  }

  function table(opts) {
    const { columns, onRowClick, rowKey, emptyTitle, emptyText, maxHeight } = opts;
    let rows = opts.rows || [];
    let sortKey = opts.sort ? opts.sort.key : null;
    let sortDir = opts.sort ? opts.sort.dir || "desc" : "desc";
    let selected = opts.selected;
    const wrap = h("div", { class: "table-wrap" + (maxHeight ? " scroll-y" : ""), style: maxHeight ? { maxHeight: maxHeight + "px" } : null });

    function cellClass(c) {
      return [c.align === "right" ? "r" : c.align === "center" ? "c" : "", c.cls || ""].join(" ").trim() || null;
    }

    function draw() {
      clear(wrap);
      if (!rows.length) {
        wrap.appendChild(empty(emptyTitle || "Nothing to show yet", emptyText));
        return;
      }
      let data = rows.slice();
      if (sortKey) {
        const col = columns.find((c) => c.key === sortKey);
        if (col) {
          const value = col.sortValue || ((r) => r[col.key]);
          data.sort((a, b) => compare(value(a), value(b)) * (sortDir === "asc" ? 1 : -1));
        }
      }
      const head = h(
        "tr",
        null,
        columns.map((c) => {
          const sortable = c.sortable !== false && !!c.key;
          const th = h(
            "th",
            { class: [cellClass(c), sortable ? "sortable" : ""].join(" ").trim() || null, style: c.width ? { width: c.width } : null, scope: "col" },
            c.label,
            sortable && sortKey === c.key ? h("span", { class: "arrow", "aria-hidden": "true" }, sortDir === "asc" ? "▲" : "▼") : null,
          );
          if (sortable) {
            th.addEventListener("click", () => {
              if (sortKey === c.key) sortDir = sortDir === "asc" ? "desc" : "asc";
              else {
                sortKey = c.key;
                sortDir = c.align === "right" ? "desc" : "asc";
              }
              draw();
            });
          }
          return th;
        }),
      );
      const body = h(
        "tbody",
        null,
        data.map((r) => {
          const key = rowKey ? rowKey(r) : null;
          const tr = h(
            "tr",
            { class: [onRowClick ? "clickable" : "", key !== null && key === selected ? "selected" : ""].join(" ").trim() || null },
            columns.map((c) => h("td", { class: cellClass(c) }, c.render ? c.render(r) : c.format ? c.format(r[c.key], r) : r[c.key])),
          );
          if (onRowClick) {
            tr.tabIndex = 0;
            const go = () => {
              selected = key;
              onRowClick(r);
              wrap.querySelectorAll("tr.selected").forEach((x) => x.classList.remove("selected"));
              tr.classList.add("selected");
            };
            tr.addEventListener("click", go);
            tr.addEventListener("keydown", (e) => {
              if (e.key === "Enter") go();
            });
          }
          return tr;
        }),
      );
      wrap.appendChild(h("table", { class: "tbl" }, h("thead", null, head), body));
    }

    draw();
    wrap.update = (next) => {
      rows = next || [];
      draw();
    };
    return wrap;
  }

  // ---------------------------------------------------------------- tooltip
  const tip = {
    el: null,
    ensure() {
      if (!this.el) {
        this.el = document.getElementById("tooltip") || h("div", { id: "tooltip", class: "tooltip", role: "tooltip" });
        if (!this.el.isConnected) document.body.appendChild(this.el);
      }
      return this.el;
    },
    show(x, y, content) {
      const el = this.ensure();
      clear(el);
      append(el, [content]);
      el.hidden = false;
      const pad = 14;
      const rect = el.getBoundingClientRect();
      let left = x + pad;
      let top = y + pad;
      if (left + rect.width > window.innerWidth - 8) left = x - rect.width - pad;
      if (top + rect.height > window.innerHeight - 8) top = y - rect.height - pad;
      el.style.left = Math.max(8, left) + "px";
      el.style.top = Math.max(8, top) + "px";
    },
    hide() {
      if (this.el) this.el.hidden = true;
    },
  };

  function ttContent(title, rows) {
    return h(
      "div",
      null,
      title ? h("div", { class: "tt-title" }, title) : null,
      rows.map((r) =>
        h(
          "div",
          { class: "tt-row" },
          h("span", { class: "tt-key" }, r.color ? h("i", { style: { background: r.color } }) : null, r.label),
          h("b", null, r.value),
        ),
      ),
    );
  }

  function attachTip(el, contentFn) {
    const show = (e) => {
      const r = el.getBoundingClientRect();
      const x = e && e.clientX !== undefined ? e.clientX : r.left + r.width / 2;
      const y = e && e.clientY !== undefined ? e.clientY : r.top;
      tip.show(x, y, contentFn());
    };
    el.addEventListener("pointermove", show);
    el.addEventListener("focus", () => show(null));
    el.addEventListener("pointerleave", () => tip.hide());
    el.addEventListener("blur", () => tip.hide());
    return el;
  }

  // ---------------------------------------------------------------- charts
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function seriesColor(i) {
    return "var(--series-" + ((i % 8) + 1) + ")";
  }

  function niceStep(range, count) {
    const raw = range / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
    return step * mag;
  }

  function niceTicks(min, max, count) {
    if (!Number.isFinite(min) || !Number.isFinite(max)) return { ticks: [0, 1], min: 0, max: 1 };
    if (max === min) max = min + (min === 0 ? 1 : Math.abs(min) * 0.1);
    const step = niceStep(max - min, count || 4);
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Number(v.toFixed(10)));
    return { ticks, min: lo, max: hi };
  }

  function measure(el, draw) {
    let last = 0;
    const run = () => {
      const w = Math.floor(el.clientWidth);
      if (w > 0 && Math.abs(w - last) >= 1) {
        last = w;
        draw(w);
      }
    };
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(run).observe(el);
    requestAnimationFrame(run);
    window.addEventListener("kit:theme", () => {
      last = 0;
      run();
    });
  }

  function legend(items) {
    return h(
      "div",
      { class: "legend" },
      items.map((it) =>
        h(
          "span",
          null,
          h("i", { class: it.type === "line" ? "line" : it.type === "dash" ? "dash" : "", style: it.type === "dash" ? { borderColor: it.color } : { background: it.color } }),
          it.label,
        ),
      ),
    );
  }

  function topRoundedBar(x, yTop, w, height, r) {
    const rr = Math.max(0, Math.min(r, w / 2, height));
    const yb = yTop + height;
    return "M" + x + "," + yb + "V" + (yTop + rr) + "Q" + x + "," + yTop + " " + (x + rr) + "," + yTop + "H" + (x + w - rr) + "Q" + (x + w) + "," + yTop + " " + (x + w) + "," + (yTop + rr) + "V" + yb + "Z";
  }

  /** Draws x-axis labels left to right, skipping any that would overlap the previous one. */
  function xLabels(svg, items, y) {
    let lastRight = -Infinity;
    for (const it of items) {
      const w = String(it.text).length * 6.4;
      const left = it.anchor === "start" ? it.x : it.anchor === "end" ? it.x - w : it.x - w / 2;
      if (left < lastRight + 8) continue;
      svg.appendChild(s("text", { x: it.x, y, "text-anchor": it.anchor }, it.text));
      lastRight = left + w;
    }
  }

  function labelEvery(labels, band, charWidth) {
    const longest = labels.reduce((m, l) => Math.max(m, String(l).length), 1);
    return Math.max(1, Math.ceil((longest * (charWidth || 6.6) + 10) / Math.max(1, band)));
  }

  /** Horizontal bars as HTML rows: label, thin bar, value at the tip. */
  function hbars(data, opts) {
    const o = opts || {};
    const format = o.format || fmt.compact;
    const max = o.max !== undefined ? o.max : Math.max(1e-9, ...data.map((d) => d.value));
    const wrap = h("div", { class: "hbars", role: "list" });
    if (!data.length) return empty(o.emptyTitle || "No data yet", o.emptyText);
    for (const d of data) {
      const row = h(
        "div",
        { class: "hbar", role: "listitem", tabindex: "0", style: o.onClick ? { cursor: "pointer" } : null },
        h("div", { class: "hbar-label", title: d.label }, d.label),
        h(
          "div",
          { class: "hbar-track" },
          h("div", { class: "hbar-fill", style: { width: Math.max(0.5, (d.value / max) * 100).toFixed(2) + "%", background: d.color || o.color || "var(--series-1)" } }),
          h("span", { class: "hbar-value" }, format(d.value, d)),
        ),
      );
      attachTip(row, () => ttContent(d.label, [{ label: o.valueLabel || "Value", value: format(d.value, d), color: d.color || o.color || "var(--series-1)" }].concat(d.extra || [])));
      if (o.onClick) row.addEventListener("click", () => o.onClick(d));
      wrap.appendChild(row);
    }
    return wrap;
  }

  /** Vertical columns, single or stacked series, with a per-band hover readout. */
  function columns(opts) {
    const { labels, series } = opts;
    const height = opts.height || 220;
    const yFormat = opts.yFormat || fmt.compact;
    const xFormat = opts.xFormat || ((l) => String(l));
    const stacked = opts.stacked !== false;
    const wrap = h("div", { class: "chart", style: { height: height + "px" } });
    measure(wrap, (W) => {
      clear(wrap);
      const padL = opts.padLeft || 44;
      const padR = 8;
      const padT = 10;
      const padB = 24;
      const n = labels.length;
      const totals = labels.map((_, i) =>
        stacked ? series.reduce((a, sr) => a + (sr.values[i] || 0), 0) : Math.max(0, ...series.map((sr) => sr.values[i] || 0)),
      );
      const { ticks, max } = niceTicks(0, Math.max(opts.yMax || 0, ...totals, 1e-9), 4);
      const plotW = Math.max(10, W - padL - padR);
      const plotH = height - padT - padB;
      const band = plotW / Math.max(1, n);
      const groupW = stacked ? Math.min(24, band * 0.62) : Math.min(24 * series.length, band * 0.8);
      const barW = stacked ? groupW : Math.max(2, groupW / series.length - 2);
      const y = (v) => padT + plotH - (v / max) * plotH;
      const svg = s("svg", { width: W, height, role: "img", "aria-label": opts.ariaLabel || "" });
      const hoverBand = s("rect", { class: "band-hover", x: 0, y: padT, width: band, height: plotH, visibility: "hidden" });
      svg.appendChild(hoverBand);
      for (const t of ticks) {
        svg.appendChild(s("line", { class: "grid-line", x1: padL, x2: W - padR, y1: y(t), y2: y(t) }));
        svg.appendChild(s("text", { x: padL - 8, y: y(t) + 4, "text-anchor": "end" }, yFormat(t)));
      }
      const every = labelEvery(labels.map(xFormat), band);
      const xItems = [];
      labels.forEach((lab, i) => {
        const x0 = padL + band * i;
        const cx = x0 + band / 2;
        if (stacked) {
          const segs = series.map((sr, si) => ({ v: sr.values[i] || 0, color: sr.color || seriesColor(si) })).filter((g) => g.v > 0);
          let acc = 0;
          segs.forEach((g, gi) => {
            const yTop = y(acc + g.v);
            const yBottom = y(acc) - (gi > 0 ? 2 : 0);
            acc += g.v;
            const hgt = Math.max(0.5, yBottom - yTop);
            const isTop = gi === segs.length - 1;
            svg.appendChild(
              isTop
                ? s("path", { d: topRoundedBar(cx - barW / 2, yTop, barW, hgt, 4), fill: g.color })
                : s("rect", { x: cx - barW / 2, y: yTop, width: barW, height: hgt, fill: g.color }),
            );
          });
        } else {
          series.forEach((sr, si) => {
            const v = sr.values[i] || 0;
            if (v <= 0) return;
            const bx = cx - groupW / 2 + si * (barW + 2) + 1;
            svg.appendChild(s("path", { d: topRoundedBar(bx, y(v), barW, Math.max(0.5, y(0) - y(v)), 4), fill: sr.color || seriesColor(si) }));
          });
        }
        if (i % every === 0) xItems.push({ x: cx, text: xFormat(lab, i), anchor: "middle" });
        const hit = s("rect", { class: "hit", x: x0, y: padT, width: band, height: plotH, tabindex: "0" });
        const rows = () => {
          const r = series.map((sr, si) => ({ label: sr.name, value: (sr.format || yFormat)(sr.values[i] || 0), color: sr.color || seriesColor(si) }));
          if (stacked && series.length > 1 && opts.showTotal !== false) r.push({ label: "Total", value: yFormat(totals[i]) });
          return r;
        };
        const show = (e) => {
          hoverBand.setAttribute("x", x0);
          hoverBand.setAttribute("visibility", "visible");
          const rect = hit.getBoundingClientRect();
          tip.show(e && e.clientX !== undefined ? e.clientX : rect.left + rect.width / 2, e && e.clientY !== undefined ? e.clientY : rect.top, ttContent(opts.tooltipTitle ? opts.tooltipTitle(lab, i) : xFormat(lab, i), rows()));
        };
        hit.addEventListener("pointermove", show);
        hit.addEventListener("focus", () => show(null));
        const hide = () => {
          hoverBand.setAttribute("visibility", "hidden");
          tip.hide();
        };
        hit.addEventListener("pointerleave", hide);
        hit.addEventListener("blur", hide);
        if (opts.onClick) hit.addEventListener("click", () => opts.onClick(lab, i));
        svg.appendChild(hit);
      });
      svg.appendChild(s("line", { class: "base-line", x1: padL, x2: W - padR, y1: y(0), y2: y(0) }));
      xLabels(svg, xItems, height - 6);
      wrap.appendChild(svg);
    });
    return wrap;
  }

  /** Multi-series line chart with a snapping crosshair and one readout for every series. */
  function line(opts) {
    const { labels, series } = opts;
    const height = opts.height || 240;
    const yFormat = opts.yFormat || fmt.compact;
    const xFormat = opts.xFormat || ((l) => String(l));
    const wrap = h("div", { class: "chart", style: { height: height + "px" } });
    measure(wrap, (W) => {
      clear(wrap);
      const padL = opts.padLeft || 48;
      const padR = opts.padRight || 12;
      const padT = 10;
      const padB = 24;
      const n = labels.length;
      const all = [];
      series.forEach((sr) => sr.values.forEach((v) => v !== null && v !== undefined && Number.isFinite(v) && all.push(v)));
      if (opts.band) {
        opts.band.lo.forEach((v) => v !== null && all.push(v));
        opts.band.hi.forEach((v) => v !== null && all.push(v));
      }
      let lo = opts.yMin !== undefined ? opts.yMin : Math.min(...all);
      let hi = opts.yMax !== undefined ? opts.yMax : Math.max(...all);
      if (opts.zero !== false && lo > 0) lo = 0;
      if (!all.length) {
        lo = 0;
        hi = 1;
      }
      const { ticks, min, max } = niceTicks(lo, hi, 4);
      const plotW = Math.max(10, W - padL - padR);
      const plotH = height - padT - padB;
      const x = (i) => padL + (n <= 1 ? plotW / 2 : (i * plotW) / (n - 1));
      const y = (v) => padT + plotH - ((v - min) / (max - min || 1)) * plotH;
      const svg = s("svg", { width: W, height, role: "img", "aria-label": opts.ariaLabel || "" });
      for (const t of ticks) {
        svg.appendChild(s("line", { class: "grid-line", x1: padL, x2: W - padR, y1: y(t), y2: y(t) }));
        svg.appendChild(s("text", { x: padL - 8, y: y(t) + 4, "text-anchor": "end" }, yFormat(t)));
      }
      const step = n <= 1 ? plotW : plotW / (n - 1);
      const every = labelEvery(labels.map(xFormat), step);
      const xItems = [];
      labels.forEach((lab, i) => {
        if (i % every === 0 || (opts.labelLast && i === n - 1)) xItems.push({ x: x(i), text: xFormat(lab, i), anchor: i === 0 && n > 1 ? "start" : i === n - 1 && n > 1 ? "end" : "middle" });
      });
      xLabels(svg, xItems, height - 6);
      if (opts.marker !== undefined && opts.marker !== null) {
        svg.appendChild(s("line", { class: "base-line", x1: x(opts.marker), x2: x(opts.marker), y1: padT, y2: padT + plotH, "stroke-dasharray": "3 3" }));
        if (opts.markerLabel) svg.appendChild(s("text", { x: x(opts.marker) + 6, y: padT + 10 }, opts.markerLabel));
      }
      if (opts.band) {
        let d = "";
        const pts = [];
        opts.band.lo.forEach((v, i) => v !== null && opts.band.hi[i] !== null && pts.push(i));
        if (pts.length > 1) {
          d = "M" + pts.map((i) => x(i) + "," + y(opts.band.hi[i])).join("L") + "L" + pts.slice().reverse().map((i) => x(i) + "," + y(opts.band.lo[i])).join("L") + "Z";
          svg.appendChild(s("path", { d, fill: opts.band.color || "var(--series-1)", "fill-opacity": "0.12", stroke: "none" }));
        }
      }
      series.forEach((sr, si) => {
        const color = sr.color || seriesColor(si);
        let d = "";
        let pen = false;
        let first = null;
        let lastIdx = null;
        sr.values.forEach((v, i) => {
          if (v === null || v === undefined || !Number.isFinite(v)) {
            pen = false;
            return;
          }
          d += (pen ? "L" : "M") + x(i).toFixed(1) + "," + y(v).toFixed(1);
          if (first === null) first = i;
          lastIdx = i;
          pen = true;
        });
        if (sr.area && first !== null) {
          svg.appendChild(s("path", { d: d + "L" + x(lastIdx) + "," + y(Math.max(min, 0)) + "L" + x(first) + "," + y(Math.max(min, 0)) + "Z", fill: color, "fill-opacity": "0.1", stroke: "none" }));
        }
        svg.appendChild(s("path", { d, fill: "none", stroke: color, "stroke-width": sr.width || 2, "stroke-linejoin": "round", "stroke-linecap": "round", "stroke-dasharray": sr.dash ? "5 4" : null }));
      });
      svg.appendChild(s("line", { class: "base-line", x1: padL, x2: W - padR, y1: padT + plotH, y2: padT + plotH }));
      const cross = s("g", { visibility: "hidden" });
      const crossLine = s("line", { class: "crosshair", y1: padT, y2: padT + plotH });
      cross.appendChild(crossLine);
      const dots = series.map((sr, si) => {
        const c = s("circle", { r: 4.5, fill: sr.color || seriesColor(si), stroke: "var(--surface)", "stroke-width": 2 });
        cross.appendChild(c);
        return c;
      });
      svg.appendChild(cross);
      const overlay = s("rect", { class: "hit", x: padL - step / 2, y: padT, width: plotW + step, height: plotH, tabindex: "0" });
      let focusIdx = n - 1;
      const showAt = (i, cx, cy) => {
        i = Math.max(0, Math.min(n - 1, i));
        focusIdx = i;
        crossLine.setAttribute("x1", x(i));
        crossLine.setAttribute("x2", x(i));
        series.forEach((sr, si) => {
          const v = sr.values[i];
          if (v === null || v === undefined || !Number.isFinite(v)) dots[si].setAttribute("visibility", "hidden");
          else {
            dots[si].setAttribute("visibility", "visible");
            dots[si].setAttribute("cx", x(i));
            dots[si].setAttribute("cy", y(v));
          }
        });
        cross.setAttribute("visibility", "visible");
        const rows = series
          .map((sr, si) => ({ sr, si, v: sr.values[i] }))
          .filter((r) => r.v !== null && r.v !== undefined && Number.isFinite(r.v))
          .map((r) => ({ label: r.sr.name, value: (r.sr.format || yFormat)(r.v), color: r.sr.color || seriesColor(r.si) }));
        if (opts.band && opts.band.lo[i] !== null && opts.band.lo[i] !== undefined) rows.push({ label: opts.band.label || "Range", value: yFormat(opts.band.lo[i]) + " – " + yFormat(opts.band.hi[i]) });
        if (opts.extraRows) rows.push(...opts.extraRows(i));
        tip.show(cx, cy, ttContent(opts.tooltipTitle ? opts.tooltipTitle(labels[i], i) : xFormat(labels[i], i), rows));
      };
      overlay.addEventListener("pointermove", (e) => {
        const r = svg.getBoundingClientRect();
        const px = e.clientX - r.left;
        const i = Math.round(((px - padL) / plotW) * (n - 1));
        showAt(n <= 1 ? 0 : i, e.clientX, e.clientY);
      });
      overlay.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
        e.preventDefault();
        const r = svg.getBoundingClientRect();
        const i = focusIdx + (e.key === "ArrowRight" ? 1 : -1);
        showAt(i, r.left + x(Math.max(0, Math.min(n - 1, i))), r.top + padT);
      });
      overlay.addEventListener("focus", () => {
        const r = svg.getBoundingClientRect();
        showAt(focusIdx, r.left + x(focusIdx), r.top + padT);
      });
      const hide = () => {
        cross.setAttribute("visibility", "hidden");
        tip.hide();
      };
      overlay.addEventListener("pointerleave", hide);
      overlay.addEventListener("blur", hide);
      svg.appendChild(overlay);
      wrap.appendChild(svg);
    });
    return wrap;
  }

  function sparkline(values, opts) {
    const o = opts || {};
    const w = o.width || 96;
    const hgt = o.height || 24;
    const clean = values.filter((v) => Number.isFinite(v));
    if (clean.length < 2) return h("span", { class: "muted small" }, "—");
    const lo = Math.min(...clean);
    const hi = Math.max(...clean);
    const x = (i) => 1 + (i * (w - 6)) / (values.length - 1);
    const y = (v) => hgt - 3 - ((v - lo) / (hi - lo || 1)) * (hgt - 6);
    let d = "";
    values.forEach((v, i) => {
      if (Number.isFinite(v)) d += (d ? "L" : "M") + x(i).toFixed(1) + "," + y(v).toFixed(1);
    });
    const last = values.length - 1;
    return s(
      "svg",
      { width: w, height: hgt, "aria-hidden": "true" },
      s("path", { d, fill: "none", stroke: "var(--series-other)", "stroke-width": 1.5, "stroke-linejoin": "round" }),
      Number.isFinite(values[last]) ? s("circle", { cx: x(last), cy: y(values[last]), r: 3, fill: o.color || "var(--series-1)" }) : null,
    );
  }

  /** Card that owns a chart plus its table-view twin. */
  function chartCard(opts) {
    const body = h("div", null);
    let mode = "chart";
    const draw = () => {
      clear(body);
      if (mode === "chart") {
        if (opts.legend && opts.legend.length) body.appendChild(legend(opts.legend));
        body.appendChild(opts.chart());
      } else {
        const t = opts.table();
        body.appendChild(table({ columns: t.columns, rows: t.rows, maxHeight: 320 }));
      }
    };
    const actions = [];
    if (opts.actions) actions.push(opts.actions);
    if (opts.table) {
      const toggle = h("button", { class: "btn btn-ghost btn-sm", type: "button", "aria-pressed": "false" }, "Table");
      toggle.addEventListener("click", () => {
        mode = mode === "chart" ? "table" : "chart";
        toggle.textContent = mode === "chart" ? "Table" : "Chart";
        toggle.setAttribute("aria-pressed", String(mode === "table"));
        draw();
      });
      actions.push(toggle);
    }
    draw();
    return card({ title: opts.title, subtitle: opts.subtitle, actions: actions.length ? actions : null, body, cls: opts.cls });
  }

  // ---------------------------------------------------------------- app shell
  function parseHash() {
    const raw = location.hash.replace(/^#\/?/, "");
    const [path, query] = raw.split("?");
    return { path: path || "", params: Object.fromEntries(new URLSearchParams(query || "")) };
  }

  function createApp(config) {
    const nav = document.getElementById("nav");
    const outlet = document.getElementById("app");
    initTheme(document.getElementById("theme-toggle"));
    const routes = config.routes;
    for (const r of routes) {
      if (!r.hidden) nav.appendChild(h("a", { href: "#/" + r.path, "data-path": r.path }, r.label));
    }
    let token = 0;
    let lastPath = null;
    async function render() {
      const { path, params } = parseHash();
      const route = routes.find((r) => r.path === path) || routes[0];
      nav.querySelectorAll("a").forEach((a) => {
        if (a.dataset.path === (route.nav || route.path)) a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
      });
      const mine = ++token;
      tip.hide();
      document.title = route.label + " · " + config.title;
      if (lastPath !== route.path) window.scrollTo(0, 0);
      lastPath = route.path;
      const view = h("div", null);
      // Views compose optional sections with `cond ? node : null`; skip those instead of
      // letting the DOM's append() print the word "null".
      view.append = (...nodes) => append(view, nodes);
      try {
        await route.render(view, params, () => mine === token);
        if (mine === token) {
          clear(outlet);
          outlet.appendChild(view);
        }
      } catch (err) {
        if (mine === token) {
          clear(outlet);
          outlet.appendChild(errorState(err));
        }
      }
    }
    window.addEventListener("hashchange", render);
    if (config.health) {
      const pill = document.getElementById("env");
      const check = async () => {
        try {
          const info = await api.get(config.health);
          if (pill) {
            pill.classList.remove("is-down");
            pill.textContent = config.healthLabel ? config.healthLabel(info) : "local · online";
          }
        } catch (_) {
          if (pill) {
            pill.classList.add("is-down");
            pill.textContent = "server offline";
          }
        }
      };
      check();
      setInterval(check, 15000);
    }
    render();
    return {
      go(path, params) {
        const q = params ? "?" + new URLSearchParams(params).toString() : "";
        location.hash = "#/" + path + q;
      },
      refresh: render,
    };
  }

  function pageHead(title, text, actions) {
    return h("div", { class: "page-head" }, h("div", null, h("h1", null, title), text ? h("p", null, text) : null), actions ? h("div", { class: "page-actions" }, actions) : null);
  }

  window.Kit = {
    h, s, clear, append, store, api, fmt,
    card, stat, badge, banner, meter, empty, field, select, seg, kv, highlight, toast, download, busy, withButton, errorState,
    table, tip, ttContent, attachTip,
    niceTicks, measure, legend, hbars, columns, line, sparkline, chartCard, seriesColor, cssVar,
    createApp, pageHead, parseHash, initTheme,
  };
})();
