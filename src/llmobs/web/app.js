/* Inference Observatory: operations console over the llmobs gateway. */
(function () {
  "use strict";
  const { h, api, fmt } = Kit;

  const SLO_LABEL = { availability: "Availability", latency_p95_5s: "Latency under 5 s", ttft_p95_1s: "First token under 1 s" };
  const SEVERITY = { ok: { kind: "good", label: "OK" }, ticket: { kind: "warning", label: "Ticket" }, page: { kind: "critical", label: "Page" } };
  const KIND_COLOR = { request: "var(--series-1)", inference: "var(--series-3)", retry: "var(--series-2)", validation: "var(--series-7)", stream: "var(--series-4)" };
  const WINDOWS = [
    { value: 300, label: "5 min" },
    { value: 900, label: "15 min" },
    { value: 3600, label: "1 hour" },
  ];
  const state = { window: 300 };

  const outcomeBadge = (r) =>
    r.outcome !== "ok" ? Kit.badge("Failed", "critical") : r.status !== "ok" ? Kit.badge("Served after retry", "warning") : Kit.badge("Served", "good");
  const sloName = (n) => SLO_LABEL[n] || n;
  const pct = (x, d) => (x === null || x === undefined ? "—" : (x * 100).toFixed(d === undefined ? 2 : d) + "%");
  const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const windowLabel = (s) => (s >= 86400 ? s / 86400 + " d" : s >= 3600 ? s / 3600 + " h" : s / 60 + " min");

  // ------------------------------------------------------------------ overview
  function alertBanner(data) {
    const hot = data.slos.filter((s) => s.severity !== "ok");
    if (!hot.length) {
      return Kit.banner({ kind: "good", title: "All objectives within budget", text: "No burn-rate window has crossed its alerting threshold." });
    }
    const worst = hot.find((s) => s.severity === "page") || hot[0];
    const cfg = data.slo_config.find((c) => c.name === worst.slo);
    const win = worst.triggered_windows[0];
    const rule = cfg && cfg.windows.find((w) => w.name === win);
    return Kit.banner({
      kind: SEVERITY[worst.severity].kind,
      title: (worst.severity === "page" ? "Paging: " : "Ticket: ") + sloName(worst.slo) + " is burning its error budget",
      text:
        (rule ? "Burn rate " + fmt.num(worst.burn_rates[win], 1) + "× over the " + windowLabel(rule.window_seconds) + " window (threshold " + rule.threshold + "×). " : "") +
        "Observed " + pct(worst.observed) + " against an objective of " + pct(worst.objective, 1) + "." +
        (hot.length > 1 ? " " + (hot.length - 1) + " more objective" + (hot.length > 2 ? "s" : "") + " alerting." : ""),
    });
  }

  function sloCard(s, cfg) {
    const sev = SEVERITY[s.severity] || SEVERITY.ok;
    const remaining = Math.max(0, s.budget_remaining);
    const what =
      cfg.kind === "availability"
        ? "Requests that got an answer"
        : (cfg.indicator === "ttft" ? "Time to first token" : "Request duration") + " within " + cfg.threshold_seconds + " s";
    return Kit.card({
      cls: "slo-card",
      title: sloName(s.slo),
      subtitle: what + " · objective " + pct(s.objective, 1),
      actions: Kit.badge(sev.label, sev.kind),
      body: h(
        "div",
        null,
        h("div", { class: "row-between" }, h("div", null, h("div", { class: "stat-value" }, pct(s.observed)), h("div", { class: "small muted" }, fmt.int(s.total_events - s.bad_events) + " of " + fmt.int(s.total_events) + " good")), h("div", { style: { textAlign: "right" } }, h("div", { class: "small muted" }, "Error budget left"), h("div", { style: { fontWeight: "600" } }, pct(remaining, 0)))),
        h("div", { style: { marginTop: "8px" } }, Kit.meter(remaining, remaining > 0.5 ? "good" : remaining > 0.2 ? "warning" : "critical", "Error budget remaining")),
        h(
          "div",
          { class: "burns" },
          cfg.windows.map((w) => {
            const v = s.burn_rates[w.name] || 0;
            return h(
              "div",
              { class: "burn" + (v >= w.threshold ? " hot" : ""), title: "Pages or tickets when the burn rate reaches " + w.threshold + "×" },
              h("div", { class: "name" }, w.name + " · " + windowLabel(w.window_seconds)),
              h("div", { class: "val" }, fmt.num(v, 1) + "×"),
              h("div", { class: "name" }, (w.severity === "page" ? "pages" : "tickets") + " at " + w.threshold + "×"),
            );
          }),
        ),
      ),
    });
  }

  function buildDashboard(data) {
    const k = data.kpis;
    const tiles = h(
      "div",
      { class: "grid cols-5" },
      Kit.stat({ label: "Throughput", value: fmt.num(k.rps, 2) + " req/s", note: fmt.int(k.requests) + " requests in " + windowLabel(data.window_seconds) }),
      Kit.stat({ label: "Failed requests", value: pct(k.error_rate), note: fmt.int(k.failed) + " failed · " + fmt.int(k.retried) + " recovered by retry" }),
      Kit.stat({ label: "Latency p95", value: fmt.ms(k.p95), note: "p50 " + fmt.ms(k.p50) + " · p99 " + fmt.ms(k.p99) }),
      Kit.stat({ label: "Time to first token p95", value: fmt.ms(k.ttft_p95), note: "objective: under 1 s" }),
      Kit.stat({ label: "Output tokens", value: fmt.num(k.tokens_per_s, 0) + "/s", note: fmt.int(k.in_flight) + " requests in flight" }),
    );
    const series = data.series;
    const labels = series.map((p) => p.t);
    const xFormat = (t) => clock(t);
    const requestsCard = Kit.chartCard({
      title: "Requests",
      subtitle: "Per " + data.bucket_seconds + " s, by outcome",
      legend: [
        { label: "Served", color: "var(--series-1)" },
        { label: "Failed", color: "var(--critical)" },
      ],
      chart: () =>
        Kit.columns({
          labels,
          series: [
            { name: "Served", color: "var(--series-1)", values: series.map((p) => p.requests - p.failed) },
            { name: "Failed", color: "var(--critical)", values: series.map((p) => p.failed) },
          ],
          height: 220,
          xFormat,
          tooltipTitle: (t) => clock(t),
          ariaLabel: "Requests per interval, served and failed",
        }),
      table: () => ({
        columns: [
          { key: "time", label: "Time" },
          { key: "requests", label: "Requests", align: "right" },
          { key: "failed", label: "Failed", align: "right" },
          { key: "retried", label: "Retried", align: "right" },
        ],
        rows: series.filter((p) => p.requests).map((p) => Object.assign({ time: clock(p.t) }, p)),
      }),
    });
    const latencyCard = Kit.chartCard({
      title: "Latency",
      subtitle: "Served requests, milliseconds",
      legend: [
        { label: "p50", color: "var(--series-1)", type: "line" },
        { label: "p95", color: "var(--series-2)", type: "line" },
        { label: "First token p95", color: "var(--series-3)", type: "line" },
      ],
      chart: () =>
        Kit.line({
          labels,
          series: [
            { name: "p50", color: "var(--series-1)", values: series.map((p) => p.p50) },
            { name: "p95", color: "var(--series-2)", values: series.map((p) => p.p95) },
            { name: "First token p95", color: "var(--series-3)", values: series.map((p) => p.ttft_p95) },
          ],
          height: 220,
          yFormat: (v) => (v >= 1000 ? fmt.num(v / 1000, 1) + " s" : Math.round(v) + " ms"),
          xFormat,
          tooltipTitle: (t) => clock(t),
          ariaLabel: "Latency percentiles over time",
        }),
      table: () => ({
        columns: [
          { key: "time", label: "Time" },
          { key: "p50", label: "p50", align: "right", format: (v) => fmt.ms(v) },
          { key: "p95", label: "p95", align: "right", format: (v) => fmt.ms(v) },
          { key: "ttft_p95", label: "TTFT p95", align: "right", format: (v) => fmt.ms(v) },
        ],
        rows: series.filter((p) => p.requests).map((p) => Object.assign({ time: clock(p.t) }, p)),
      }),
    });
    const stages = data.stages.map((s) => ({ label: fmt.title(s.kind), value: s.mean_ms, color: KIND_COLOR[s.kind], extra: [{ label: "Share of request time", value: pct(s.share, 1) }] }));
    const stagesCard = Kit.chartCard({
      title: "Where the time goes",
      subtitle: "Mean exclusive time per stage, so the stages add up to the request",
      chart: () => Kit.hbars(stages, { format: (v, d) => fmt.ms(v) + "  ·  " + d.extra[0].value, valueLabel: "Mean", emptyTitle: "No traces in this window" }),
      table: () => ({ columns: [{ key: "label", label: "Stage" }, { key: "value", label: "Mean", align: "right", format: (v) => fmt.ms(v) }], rows: stages }),
    });
    const b = data.backend;
    const backendCard = Kit.card({
      title: "Backend",
      subtitle: b.detail || "",
      actions: b.up ? Kit.badge("Up", "good") : Kit.badge("Down", "critical"),
      body: h(
        "div",
        { class: "stack", style: { gap: "10px" } },
        Kit.kv([
          ["Model", h("code", null, b.model || "—")],
          ["Running batch", fmt.int(b.running_batch_size)],
          ["Queue depth", fmt.int(b.queue_depth)],
        ]),
        h("div", null, h("div", { class: "row-between small" }, h("span", { class: "muted" }, "Batch slots in use"), h("span", { class: "num" }, pct(b.kv_cache_utilization, 0))), Kit.meter(b.kv_cache_utilization, b.kv_cache_utilization > 0.9 ? "warning" : "", "Batch slots in use")),
      ),
    });
    return h(
      "div",
      null,
      alertBanner(data),
      h("div", { class: "mt" }, tiles),
      h("div", { class: "grid cols-2 mt" }, requestsCard, latencyCard),
      h("div", { class: "grid cols-3 mt" }, data.slos.map((s) => sloCard(s, data.slo_config.find((c) => c.name === s.slo)))),
      h("div", { class: "split-r mt" }, stagesCard, backendCard),
    );
  }

  async function overview(view, params, alive) {
    let data = await api.get("/v1/ui/overview?window=" + state.window);
    const content = h("div", null);
    const live = h("span", { class: "live" }, h("i"), "Live");

    const traffic = () => data.traffic;
    const trafficBtn = h("button", { class: "btn btn-sm", type: "button" });
    const rates = [...new Set([1, 2, 3, 5, 8, 12, traffic().rps])].sort((x, y) => x - y);
    const rpsSel = Kit.select(rates.map((v) => ({ value: v, label: v + " req/s" })), traffic().rps, (v) => setTraffic(true, Number(v)), { style: { width: "96px", height: "28px" }, "aria-label": "Traffic rate" });
    const paintTraffic = () => {
      trafficBtn.textContent = traffic().running ? "Pause traffic" : "Start traffic";
      live.classList.toggle("off", !traffic().running);
      live.lastChild.textContent = traffic().running ? "Live · " + traffic().rps + " req/s" : "Traffic paused";
    };
    const setTraffic = async (running, rps) => {
      try {
        data.traffic = await api.post("/v1/ui/traffic", { running, rps: rps || Number(rpsSel.value) });
        paintTraffic();
      } catch (err) {
        Kit.toast(err.message, "error");
      }
    };
    trafficBtn.addEventListener("click", () => setTraffic(!traffic().running));
    const scenarioNote = h("span", { class: "small muted" });
    const paintScenario = () => {
      const sc = traffic().scenarios.find((x) => x.name === traffic().scenario);
      scenarioNote.textContent = sc ? sc.detail : "";
    };
    const scenarioSeg = traffic().simulated
      ? Kit.seg(traffic().scenarios.map((s) => ({ value: s.name, label: s.label })), traffic().scenario, async (v) => {
          try {
            data.traffic = await api.post("/v1/ui/scenario", { scenario: v });
            paintScenario();
            Kit.toast("Backend scenario: " + traffic().scenarios.find((x) => x.name === v).label);
          } catch (err) {
            Kit.toast(err.message, "error");
          }
        })
      : h("span", { class: "small muted" }, "Real backend: scenarios disabled");

    const controls = h(
      "div",
      { class: "controls" },
      h("div", { class: "group" }, h("span", null, "Window"), Kit.seg(WINDOWS, state.window, (v) => {
        state.window = v;
        refresh(true);
      })),
      h("div", { class: "group" }, h("span", null, "Traffic"), trafficBtn, rpsSel),
      h("div", { class: "group" }, h("span", null, "Backend"), scenarioSeg),
      live,
    );

    let timer = null;
    const refresh = async (force) => {
      if (!alive()) {
        clearInterval(timer);
        return;
      }
      if (!force && (document.hidden || content.querySelector(".chart:hover, .hbar:hover"))) return;
      try {
        data = await api.get("/v1/ui/overview?window=" + state.window);
        if (!alive()) return;
        Kit.tip.hide();
        content.replaceChildren(buildDashboard(data));
        paintTraffic();
      } catch (err) {
        live.classList.add("off");
        live.lastChild.textContent = "Disconnected";
      }
    };

    view.append(
      Kit.pageHead("Overview", "Live view of the gateway: every number comes from its trace store and SLO tracker, the same data Prometheus scrapes."),
      controls,
      h("div", { class: "small muted", style: { marginTop: "-8px", marginBottom: "14px" } }, scenarioNote),
      content,
    );
    content.append(buildDashboard(data));
    paintTraffic();
    paintScenario();
    timer = setInterval(() => refresh(false), 2000);
  }

  // ------------------------------------------------------------------ traces
  function waterfall(trace) {
    const spans = trace.spans.slice().sort((a, b) => a.started_at - b.started_at);
    const root = spans.find((s) => !s.parent_id) || spans[0];
    const t0 = root.started_at;
    const total = Math.max(trace.duration_ms || 0, ...spans.map((s) => (s.started_at - t0) * 1000 + (s.duration_ms || 0)), 0.001);
    const depth = (s) => {
      let d = 0;
      let cur = s;
      while (cur && cur.parent_id) {
        cur = spans.find((x) => x.span_id === cur.parent_id);
        d++;
      }
      return d;
    };
    return h(
      "div",
      { class: "wf" },
      spans.map((s) => {
        const left = (((s.started_at - t0) * 1000) / total) * 100;
        const width = Math.max(0.4, ((s.duration_ms || 0) / total) * 100);
        const bar = h("div", { class: "wf-bar", style: { left: left.toFixed(2) + "%", width: Math.min(width, 100 - left).toFixed(2) + "%", background: s.status === "error" ? "var(--critical)" : KIND_COLOR[s.kind] || "var(--series-other)" } });
        const row = h(
          "div",
          { class: "wf-row", tabindex: "0" },
          h("div", { class: "wf-name", style: { paddingLeft: depth(s) * 14 + "px" }, title: s.name }, s.name, s.status === "error" ? h("span", null, " ", Kit.badge("error", "critical")) : null),
          h("div", { class: "wf-track" }, bar),
          h("div", { class: "num small", style: { textAlign: "right" } }, fmt.ms(s.duration_ms)),
        );
        Kit.attachTip(row, () =>
          Kit.ttContent(s.name, [
            { label: "Kind", value: s.kind, color: KIND_COLOR[s.kind] },
            { label: "Starts at", value: "+" + fmt.ms((s.started_at - t0) * 1000) },
            { label: "Duration", value: fmt.ms(s.duration_ms) },
            s.ttft_ms !== null && s.ttft_ms !== undefined ? { label: "First token", value: fmt.ms(s.ttft_ms) } : null,
            s.error ? { label: "Error", value: s.error } : null,
          ].filter(Boolean)),
        );
        return row;
      }),
    );
  }

  async function showTrace(host, traceId) {
    const t = await Kit.busy(host, () => api.get("/v1/traces/" + traceId));
    const root = t.spans.find((s) => !s.parent_id) || t.spans[0];
    const attrs = root.attributes || {};
    const kinds = [...new Set(t.spans.map((s) => s.kind))];
    const breakdown = Object.entries(t.breakdown_ms || {});
    const sum = breakdown.reduce((a, [, v]) => a + v, 0) || 1;
    const events = t.spans.flatMap((s) => (s.events || []).map((e) => ({ span: s.name, ...e })));
    Kit.clear(host);
    host.append(
      Kit.card({
        title: "Trace " + fmt.short(t.trace_id, 12),
        subtitle: new Date(root.started_at * 1000).toLocaleString() + " · " + t.span_count + " spans",
        actions: outcomeBadge({ outcome: root.status, status: t.status }),
        body: h(
          "div",
          { class: "stack" },
          Kit.kv([
            ["Duration", fmt.ms(t.duration_ms) + (root.ttft_ms ? " · first token " + fmt.ms(root.ttft_ms) : "")],
            ["Model", h("code", null, root.model || "—")],
            ["Attempts", String(attrs.attempts || 1)],
            ["Tokens", fmt.int(t.usage.prompt_tokens) + " prompt (" + fmt.int(t.usage.cached_prompt_tokens) + " cached) · " + fmt.int(t.usage.completion_tokens) + " completion"],
            ["Valid response", attrs.valid === false ? Kit.badge("No", "warning") : Kit.badge("Yes", "good")],
          ]),
          h("div", null, Kit.legend(kinds.map((k) => ({ label: fmt.title(k), color: KIND_COLOR[k] || "var(--series-other)" })).concat([{ label: "Failed span", color: "var(--critical)" }])), waterfall(t)),
          breakdown.length
            ? h(
                "div",
                null,
                h("div", { class: "small muted", style: { marginBottom: "6px" } }, "Exclusive time by stage"),
                h("div", { class: "breakdown-bar" }, breakdown.map(([k, v]) => {
                  const seg = h("i", { style: { width: ((v / sum) * 100).toFixed(2) + "%", background: KIND_COLOR[k] || "var(--series-other)" }, tabindex: "0" });
                  Kit.attachTip(seg, () => Kit.ttContent(fmt.title(k), [{ label: "Exclusive time", value: fmt.ms(v), color: KIND_COLOR[k] }, { label: "Share", value: pct(v / sum, 1) }]));
                  return seg;
                })),
                h("div", { class: "row small ink-2", style: { marginTop: "6px", gap: "4px 14px" } }, breakdown.map(([k, v]) => h("span", null, fmt.title(k) + " " + fmt.ms(v)))),
              )
            : null,
          events.length
            ? Kit.table({
                columns: [
                  { key: "name", label: "Event", render: (e) => h("code", null, e.name) },
                  { key: "span", label: "Span" },
                  { key: "timestamp", label: "At", align: "right", render: (e) => "+" + fmt.ms((e.timestamp - root.started_at) * 1000) },
                ],
                rows: events,
              })
            : null,
        ),
      }),
    );
  }

  async function traces(view, params) {
    let viewName = params.view || "recent";
    const detail = h("div", null, Kit.card({ body: Kit.empty("Select a trace", "Its span waterfall, retries and token accounting appear here.") }));
    const tableHost = h("div", null);
    const load = async () => {
      const rows = await Kit.busy(tableHost, () => api.get("/v1/ui/traces?limit=150&view=" + viewName));
      Kit.clear(tableHost);
      tableHost.append(
        Kit.table({
          columns: [
            { key: "started_at", label: "Time", render: (r) => h("span", { class: "num nowrap" }, clock(r.started_at)) },
            { key: "outcome", label: "Outcome", render: outcomeBadge, sortValue: (r) => (r.outcome !== "ok" ? 2 : r.status !== "ok" ? 1 : 0) },
            { key: "duration_ms", label: "Duration", align: "right", format: (v) => fmt.ms(v) },
            { key: "ttft_ms", label: "First token", align: "right", cls: "hide-sm", format: (v) => fmt.ms(v) },
            { key: "completion_tokens", label: "Tokens", align: "right", cls: "hide-sm", format: (v) => fmt.int(v) },
            { key: "valid", label: "Valid", cls: "hide-sm", render: (r) => (r.valid ? "Yes" : Kit.badge("No", "warning")) },
          ],
          rows,
          rowKey: (r) => r.trace_id,
          maxHeight: 620,
          onRowClick: (r) => showTrace(detail, r.trace_id),
          emptyTitle: "No traces match",
          emptyText: "Start traffic from the overview, or switch the backend to a failing scenario.",
        }),
      );
      if (rows.length && params.trace === undefined) showTrace(detail, rows[0].trace_id);
    };
    const filter = Kit.seg(
      [
        { value: "recent", label: "Recent" },
        { value: "slowest", label: "Slowest" },
        { value: "failed", label: "Failed" },
        { value: "retried", label: "Retried" },
        { value: "invalid", label: "Invalid" },
      ],
      viewName,
      (v) => {
        viewName = v;
        load();
      },
    );
    view.append(
      Kit.pageHead("Traces", "Every request is a trace of spans: the request, each backend attempt, and validation. Pick one to see where its time went.", [h("button", { class: "btn", type: "button", onclick: load }, "Refresh")]),
      h("div", { class: "split-r" }, Kit.card({ title: "Requests", subtitle: "Last 15 minutes", actions: filter, flush: true, body: tableHost }), detail),
    );
    await load();
    if (params.trace) showTrace(detail, params.trace);
  }

  // ------------------------------------------------------------------ load test
  async function loadTest(view) {
    const history = await api.get("/v1/ui/load");
    const requests = h("input", { class: "input", type: "number", min: "1", max: "2000", value: "60" });
    const concurrency = h("input", { class: "input", type: "number", min: "1", max: "64", value: "12" });
    const tokens = Kit.select([64, 96, 160, 256], 96);
    const stream = h("input", { type: "checkbox", checked: true });
    const run = h("button", { class: "btn btn-primary", type: "submit" }, "Run load test");
    const result = h("div", null);
    const historyHost = h("div", null);

    const drawHistory = (rows) => {
      Kit.clear(historyHost);
      historyHost.append(
        Kit.card({
          title: "Run history",
          subtitle: "This session, newest first",
          flush: true,
          body: Kit.table({
            columns: [
              { key: "started_at", label: "Time", render: (r) => clock(r.started_at) },
              { key: "scenario", label: "Backend", render: (r) => fmt.title(r.scenario) },
              { key: "requests", label: "Requests", align: "right" },
              { key: "concurrency", label: "Concurrency", align: "right" },
              { key: "stream", label: "Mode", cls: "hide-sm", render: (r) => (r.stream ? "Streamed" : "Batch") },
              { key: "throughput_rps", label: "req/s", align: "right", format: (v) => fmt.num(v, 1) },
              { key: "p50", label: "p50", align: "right", render: (r) => fmt.ms(r.latency_ms.p50), sortValue: (r) => r.latency_ms.p50 },
              { key: "p95", label: "p95", align: "right", render: (r) => fmt.ms(r.latency_ms.p95), sortValue: (r) => r.latency_ms.p95 },
              { key: "ttft", label: "TTFT p95", align: "right", cls: "hide-sm", render: (r) => fmt.ms(r.ttft_ms.p95), sortValue: (r) => r.ttft_ms.p95 },
              { key: "error_rate", label: "Errors", align: "right", format: (v) => pct(v, 1) },
            ],
            rows,
            emptyTitle: "No load tests yet",
          }),
        }),
      );
    };

    const form = h(
      "form",
      { class: "grid cols-4", style: { alignItems: "end" } },
      Kit.field("Requests", requests),
      Kit.field("Concurrency", concurrency),
      Kit.field("Max tokens", tokens),
      h("div", { class: "stack", style: { gap: "8px" } }, h("label", { class: "row small" }, stream, "Stream responses"), run),
    );
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      Kit.withButton(run, async () => {
        run.textContent = "Running…";
        const r = await Kit.busy(result, () => api.post("/v1/ui/load", { requests: Number(requests.value), concurrency: Number(concurrency.value), max_tokens: Number(tokens.value), stream: stream.checked }));
        Kit.clear(result);
        result.append(
          h(
            "div",
            { class: "grid cols-5 mt" },
            Kit.stat({ label: "Throughput", value: fmt.num(r.throughput_rps, 1) + " req/s", note: r.requests + " requests in " + fmt.num(r.duration_seconds, 1) + " s" }),
            Kit.stat({ label: "Latency p95", value: fmt.ms(r.latency_ms.p95), note: "p50 " + fmt.ms(r.latency_ms.p50) + " · p99 " + fmt.ms(r.latency_ms.p99) }),
            Kit.stat({ label: "First token p95", value: fmt.ms(r.ttft_ms.p95), note: "p50 " + fmt.ms(r.ttft_ms.p50) }),
            Kit.stat({ label: "Errors", value: pct(r.error_rate, 1), note: r.failures + " failed · " + r.shed + " shed" }),
            Kit.stat({ label: "Output tokens", value: fmt.num(r.output_tokens_per_second, 0) + "/s", note: "cache " + pct(r.tokens.cache_hit_rate, 1) }),
          ),
        );
        drawHistory(await api.get("/v1/ui/load"));
        Kit.toast("Load test finished: " + fmt.num(r.throughput_rps, 1) + " req/s, p95 " + fmt.ms(r.latency_ms.p95));
      });
    });
    view.append(
      Kit.pageHead("Load test", "Drive a burst of concurrent requests through the gateway. It runs against the live backend and scenario, so the overview and SLOs see it too."),
      Kit.card({ body: form }),
      result,
      h("div", { class: "mt" }, historyHost),
    );
    drawHistory(history);
  }

  // ------------------------------------------------------------------ playground
  async function playground(view) {
    const prompt = h("textarea", { class: "textarea", rows: "4", "aria-label": "Prompt" });
    prompt.value = "You are a helpful support assistant for an enterprise SaaS product. Answer concisely and cite the relevant policy section.\n\nWhat is the refund window for annual subscriptions?";
    const maxTokens = Kit.select([32, 64, 96, 160, 256], 96);
    const expectJson = h("input", { type: "checkbox" });
    const streaming = h("input", { type: "checkbox", checked: true });
    const mustContain = h("input", { class: "input", placeholder: "comma-separated, optional" });
    const send = h("button", { class: "btn btn-primary", type: "submit" }, "Send");
    const out = h("div", null, Kit.card({ body: Kit.empty("Send a request", "The response comes back with its trace, timing breakdown and validation result.") }));
    const form = h(
      "form",
      { class: "stack" },
      Kit.field("Prompt", prompt),
      h("div", { class: "grid cols-2" }, Kit.field("Max tokens", maxTokens), Kit.field("Must contain", mustContain)),
      h("div", { class: "row", style: { gap: "18px" } }, h("label", { class: "row small" }, streaming, "Stream the response"), h("label", { class: "row small" }, expectJson, "Validate that the response is JSON")),
      h("div", { class: "row-between" }, h("span", { class: "small muted" }, "Simulated backend: timing, tokens and failures are modelled; the text is placeholder."), send),
    );
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      Kit.withButton(send, async () => {
        send.textContent = "Generating…";
        const r = await Kit.busy(out, () =>
          api.post("/v1/generate", {
            prompt: prompt.value,
            max_tokens: Number(maxTokens.value),
            stream: streaming.checked,
            expect_json: expectJson.checked,
            must_contain: mustContain.value.split(",").map((x) => x.trim()).filter(Boolean),
          }),
        );
        const issues = (r.validation && r.validation.issues) || [];
        const parts = Object.entries(r.breakdown_ms || {});
        const sum = parts.reduce((a, [, v]) => a + v, 0) || 1;
        Kit.clear(out);
        out.append(
          Kit.card({
            title: "Response",
            subtitle: "Trace " + fmt.short(r.trace_id, 12) + " · " + r.model,
            actions: h("a", { class: "btn btn-sm", href: "#/traces?trace=" + r.trace_id }, "Open trace"),
            body: h(
              "div",
              { class: "stack" },
              h("div", { class: "row" }, r.valid ? Kit.badge("Valid", "good") : Kit.badge("Failed validation", "warning"), h("span", { class: "small muted" }, fmt.ms(r.duration_ms) + " total · first token " + fmt.ms(r.ttft_ms) + " · " + r.attempts + " attempt" + (r.attempts > 1 ? "s" : ""))),
              h("div", { class: "codebox" }, r.text),
              h("div", null, h("div", { class: "small muted", style: { marginBottom: "6px" } }, "Time by stage"), h("div", { class: "breakdown-bar" }, parts.map(([k, v]) => h("i", { style: { width: ((v / sum) * 100).toFixed(2) + "%", background: KIND_COLOR[k] || "var(--series-other)" }, title: k + " " + fmt.ms(v) }))), h("div", { class: "row small ink-2", style: { marginTop: "6px", gap: "4px 14px" } }, parts.map(([k, v]) => h("span", null, fmt.title(k) + " " + fmt.ms(v))))),
              Kit.kv([
                ["Prompt tokens", fmt.int(r.usage.prompt_tokens) + " (" + fmt.int(r.usage.cached_prompt_tokens) + " served from prefix cache)"],
                ["Completion tokens", fmt.int(r.usage.completion_tokens)],
              ]),
              Kit.table({
                columns: [
                  { key: "check", label: "Check", render: (i) => h("code", null, i.check) },
                  { key: "severity", label: "Severity", render: (i) => Kit.badge(i.severity, i.severity === "fail" ? "critical" : "warning") },
                  { key: "message", label: "Finding" },
                ],
                rows: issues,
                emptyTitle: "No validation issues",
              }),
            ),
          }),
        );
      });
    });
    view.append(Kit.pageHead("Playground", "Send one request through the instrumented gateway, exactly as an application would."), h("div", { class: "split" }, Kit.card({ title: "Request", body: form }), out));
  }

  const app = Kit.createApp({
    title: "Inference Observatory",
    health: "/health",
    healthLabel: (info) => info.backend + " · v" + info.version,
    routes: [
      { path: "overview", label: "Overview", render: overview },
      { path: "traces", label: "Traces", render: traces },
      { path: "load", label: "Load test", render: loadTest },
      { path: "playground", label: "Playground", render: playground },
    ],
  });
})();
