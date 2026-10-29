/* patobase: pegamento entre htmx, Alpine, CodeMirror y Vega-Lite. */
(() => {
  "use strict";

  let editor = null;

  // ------------------------------------------------------------------ utils
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const patobase = {
    insert(text) {
      if (!editor) return;
      editor.replaceSelection(text);
      editor.focus();
    },
    selectFrom(table) {
      if (!editor) return;
      editor.setValue(`SELECT *\nFROM ${table}\nLIMIT 100`);
      editor.focus();
      htmx.trigger("#run-form", "submit");
    },
    post(url, data) {
      const body = new URLSearchParams(data);
      return fetch(url, { method: "POST", body, headers: { "HX-Request": "true" } });
    },
    toast(message, kind = "ok") {
      const area = document.getElementById("toast");
      if (!area) return;
      const div = document.createElement("div");
      div.className = `toast ${kind}`;
      div.textContent = message;
      area.replaceChildren(div);
      setTimeout(() => div.remove(), kind === "error" ? 6000 : 3500);
    },
    // Descarga un export. Con `url` exporta una pregunta guardada; sin ella, el
    // resultado del panel donde está el menú (su SQL va en data-sql).
    async exportData(fmt, el, url = null) {
      const menu = el.closest("details");
      if (menu) menu.open = false;
      const summary = menu && menu.querySelector("summary");
      let req;
      if (url) {
        req = fetch(url);
      } else {
        const panel = el.closest(".result-panel");
        const titleInput = document.querySelector("#save-form [name=name]");
        const body = new URLSearchParams({
          sql: panel ? panel.dataset.sql : "",
          title: (titleInput && titleInput.value.trim()) || "Pregunta sin título",
        });
        req = fetch(`/export/${fmt}`, { method: "POST", body });
      }
      summary && summary.setAttribute("aria-busy", "true");
      try {
        const r = await req;
        if (!r.ok) {
          patobase.toast(await r.text(), "error");
          return;
        }
        const blob = await r.blob();
        const cd = r.headers.get("Content-Disposition") || "";
        const m = cd.match(/filename\*=UTF-8''([^;]+)/) || cd.match(/filename="([^"]+)"/);
        const name = m ? decodeURIComponent(m[1]) : `patobase.${fmt}`;
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
        patobase.toast(`Descargado: ${name}`);
      } catch (e) {
        patobase.toast("No se pudo exportar: error de red.", "error");
      } finally {
        summary && summary.removeAttribute("aria-busy");
      }
    },
  };
  window.patobase = patobase;

  // ------------------------------------------------------------ CodeMirror
  function setupEditor() {
    const ta = document.getElementById("sql-editor");
    if (!ta || !window.CodeMirror) return;
    const run = () => htmx.trigger("#run-form", "submit");
    editor = CodeMirror.fromTextArea(ta, {
      mode: "text/x-sql",
      lineNumbers: true,
      matchBrackets: true,
      indentWithTabs: false,
      tabSize: 2,
      viewportMargin: Infinity,
      extraKeys: {
        "Ctrl-Enter": run,
        "Cmd-Enter": run,
        "Ctrl-Space": "autocomplete",
      },
    });
    // Autocompletado con tablas y columnas reales
    fetch("/api/completions")
      .then((r) => r.json())
      .then((tables) => editor.setOption("hintOptions", { tables, completeSingle: false }))
      .catch(() => {});
    // Sugerir al escribir tras un punto
    editor.on("inputRead", (cm, change) => {
      if (change.text[0] === ".") cm.showHint({ completeSingle: false });
    });
  }

  // Inyecta el SQL del editor y la configuración del gráfico en cada petición
  const currentViz = () => {
    const viz = window.Alpine && Alpine.store("viz").config;
    if (viz) return JSON.stringify(viz);
    const init = document.getElementById("initial-viz");
    return init ? init.textContent : "";
  };

  document.addEventListener("htmx:configRequest", (evt) => {
    // Editor visual: se envía la especificación; el SQL lo genera el servidor
    if (evt.detail.elt.closest("[data-builder]") && window.patobaseBuilder) {
      evt.detail.parameters["spec"] = JSON.stringify(window.patobaseBuilder.spec);
      evt.detail.parameters["viz"] = currentViz();
      return;
    }
    const form = evt.detail.elt.closest("[data-with-sql]");
    if (!form) return;
    if (editor) evt.detail.parameters["sql"] = editor.getValue();
    delete evt.detail.parameters["sql_src"];
    const viz = window.Alpine && Alpine.store("viz").config;
    if (viz) {
      evt.detail.parameters["viz"] = JSON.stringify(viz);
    } else {
      const init = document.getElementById("initial-viz");
      if (init) evt.detail.parameters["viz"] = init.textContent;
    }
  });

  // Errores de red / servidor: mensaje visible en vez de silencio
  document.addEventListener("htmx:responseError", (evt) => {
    const toast = document.getElementById("toast");
    if (toast) toast.innerHTML = `<div class="toast error">Error ${evt.detail.xhr.status} del servidor</div>`;
  });

  // ------------------------------------------------------------- Vega-Lite
  const ES_NUMBER = { decimal: ",", thousands: ".", grouping: [3], currency: ["", " €"] };
  const ES_TIME = {
    dateTime: "%A, %e de %B de %Y, %X", date: "%d/%m/%Y", time: "%H:%M:%S", periods: ["AM", "PM"],
    days: ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"],
    shortDays: ["dom", "lun", "mar", "mié", "jue", "vie", "sáb"],
    months: ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"],
    shortMonths: ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"],
  };

  const PALETTE = ["#d4890b", "#2f6fdb", "#2a9d6f", "#c2453d", "#8a5cd6", "#1b9aaa", "#b8664a", "#6c7a89"];

  function buildSpec(config, colTypes, values) {
    const { type, x, y, color, agg } = config;
    const yEnc = agg === "count"
      ? { aggregate: "count", type: "quantitative", title: "Recuento" }
      : { field: y, type: colTypes[y] || "quantitative", ...(agg ? { aggregate: agg } : {}) };

    const base = {
      $schema: "https://vega.github.io/schema/vega-lite/v5.json",
      data: { values },
      width: "container",
      height: 300,
      autosize: { type: "fit", contains: "padding" },
    };

    if (type === "arc") {
      return {
        ...base,
        mark: { type: "arc", innerRadius: 60, stroke: css("--bg"), strokeWidth: 2 },
        encoding: {
          theta: { ...yEnc, stack: true },
          color: { field: x, type: "nominal", title: x },
          tooltip: [{ field: x }, yEnc],
        },
      };
    }

    const xType = colTypes[x] || "nominal";
    const enc = {
      x: { field: x, type: xType, ...(xType === "nominal" ? { sort: null, axis: { labelAngle: -30 } } : {}) },
      y: yEnc,
      tooltip: [{ field: x, type: xType }, yEnc],
    };
    if (color) {
      enc.color = { field: color, type: colTypes[color] === "quantitative" ? "quantitative" : "nominal" };
      enc.tooltip.push({ field: color });
    }
    const mark = { type, tooltip: true };
    if (type === "line") Object.assign(mark, { point: values.length <= 60, strokeWidth: 2 });
    if (type === "area") Object.assign(mark, { opacity: 0.75, line: true });
    if (type === "bar") Object.assign(mark, { cornerRadiusEnd: 2 });
    if (type === "point") Object.assign(mark, { filled: true, opacity: 0.7 });
    return { ...base, mark, encoding: enc };
  }

  function vegaConfig() {
    const fg = css("--muted"), grid = css("--line");
    return {
      background: null,
      font: css("--font"),
      view: { stroke: null },
      axis: { labelColor: fg, titleColor: fg, gridColor: grid, domainColor: grid, tickColor: grid, labelFontSize: 11, titleFontWeight: 500 },
      legend: { labelColor: fg, titleColor: fg },
      range: { category: PALETTE },
      mark: { color: PALETTE[0] },
    };
  }

  async function drawChart(el, config, colTypes, values) {
    if (!el || config.type === "table" || !window.vegaEmbed) return;
    if (!values.length) { el.innerHTML = '<p class="muted small">Sin datos que dibujar.</p>'; return; }
    try {
      await vegaEmbed(el, buildSpec(config, colTypes, values), {
        actions: { export: true, source: false, compiled: false, editor: false },
        renderer: "svg",
        config: vegaConfig(),
        i18n: { PNG_ACTION: "Descargar PNG", SVG_ACTION: "Descargar SVG" },
        formatLocale: ES_NUMBER,
        timeFormatLocale: ES_TIME,
      });
    } catch (e) {
      el.innerHTML = `<p class="error-text small">No se puede dibujar: ${e.message}</p>`;
    }
  }

  // ---------------------------------------------------------------- Alpine
  document.addEventListener("alpine:init", () => {
    Alpine.store("viz", { config: null });
    Alpine.store("dash", { editing: false });

    // Panel de resultados del editor: tabla + gráfico configurable
    Alpine.data("vizPanel", (config, colTypes) => ({
      config: { agg: "", color: "", ...config },
      colTypes,
      columns: Object.keys(colTypes),
      lastChart: config.type === "table" ? "bar" : config.type,
      values: [],
      init() {
        this.values = JSON.parse(this.$refs.data.textContent);
        Alpine.store("viz").config = this.config;
        this.$watch("config", () => {
          if (this.config.type !== "table") this.lastChart = this.config.type;
          Alpine.store("viz").config = this.config;
          this.$nextTick(() => this.draw());
        });
        this.$nextTick(() => this.draw());
      },
      draw() { drawChart(this.$refs.chart, this.config, this.colTypes, this.values); },
    }));

    // Gráfico de solo lectura (tarjetas de dashboard)
    Alpine.data("vizChart", (config, colTypes) => ({
      init() {
        const values = JSON.parse(this.$refs.data.textContent);
        this.$nextTick(() => drawChart(this.$refs.chart, { agg: "", color: "", ...config }, colTypes, values));
      },
    }));

    // Controles del dashboard: refresco manual y automático
    Alpine.data("dashControls", () => ({
      every: 0,
      timer: null,
      refresh() { htmx.trigger(document.body, "dash-refresh"); },
      schedule() {
        clearInterval(this.timer);
        if (this.every > 0) this.timer = setInterval(() => this.refresh(), this.every * 1000);
      },
    }));
  });

  // Al cambiar el tema del sistema, redibujar con los colores nuevos
  // ------------------------------------------------------- editor visual
  const emptySpec = (source = "") => ({
    source, joins: [], filters: [], metrics: [], groups: [], columns: [], sort: [], limit: null,
  });

  document.addEventListener("alpine:init", () => {
    Alpine.data("builder", (initial, catalog) => ({
      catalog,
      spec: { ...emptySpec(), ...initial },
      compiled: { sql: "", columns: [], error: "" },
      showSql: false,
      showColumnsStep: false,
      running: false,
      _timer: null,

      init() {
        window.patobaseBuilder = this;
        this.$watch("spec", () => {
          clearTimeout(this._timer);
          this._timer = setTimeout(() => this.compile(), 250);
        });
        this.compile().then(() => { if (initial && initial.source && document.getElementById("initial-viz")) this.run(); });
      },

      get showColumns() {
        return !this.spec.metrics.length && !this.spec.groups.length && (this.showColumnsStep || this.spec.columns.length > 0);
      },

      // ---- catálogo
      tableColumns(key) { return (this.catalog.tables[key] || {}).columns || []; },
      tableName(key) { return (this.catalog.tables[key] || {}).name || key; },
      tables() { return [this.spec.source, ...this.spec.joins.map((j) => j.table)]; },
      columnsUpTo(i) {
        // columnas de las tablas anteriores a la unión i (t0..ti)
        return this.tables().slice(0, i + 1).flatMap((key, idx) => this.columnsOf(key, idx));
      },
      columnsOf(key, idx) {
        const multi = this.spec.joins.length > 0;
        return this.tableColumns(key).map(([name, type]) => ({
          ref: `t${idx}.${name}`, type,
          label: multi ? `${this.tableName(key)} · ${name}` : name,
        }));
      },
      allColumns() { return this.tables().flatMap((key, idx) => (key ? this.columnsOf(key, idx) : [])); },
      typeOf(ref) { const c = this.allColumns().find((x) => x.ref === ref); return c ? c.type : "nominal"; },
      isTemporal(ref) { return this.typeOf(ref) === "temporal"; },
      opsFor(ref) { return this.catalog.opsByType[this.typeOf(ref)] || []; },
      needsValue(op) { return !!op && !this.catalog.noValue.includes(op); },
      inputType(f) {
        if (f.op === "in" || f.op === "last_days") return f.op === "last_days" ? "number" : "text";
        const t = this.typeOf(f.col);
        return t === "temporal" ? "date" : t === "quantitative" ? "number" : "text";
      },
      placeholder(f) {
        if (f.op === "in") return "valor1, valor2, …";
        if (f.op === "last_days") return "días";
        return "valor";
      },
      metricColumns(agg) {
        const need = this.catalog.aggNeeds[agg];
        return this.allColumns().filter((c) => !need || c.type === need);
      },
      firstColumn(pred = () => true) { const c = this.allColumns().find(pred); return c ? c.ref : ""; },

      // ---- pasos
      resetFromSource() {
        const src = this.spec.source;
        Object.assign(this.spec, emptySpec(src));
        this.showColumnsStep = false;
      },
      guessJoin(j, i) {
        // Propone la condición de unión: una columna con el mismo nombre en ambos lados,
        // prefiriendo identificadores (cliente_id = cliente_id, id = id…)
        const right = this.tableColumns(j.table).map((c) => c[0]);
        const left = this.columnsUpTo(i);
        const score = (name) => (name.endsWith("_id") ? 2 : name === "id" ? 1 : 0);
        const pairs = left
          .map((c) => ({ ref: c.ref, name: c.ref.split(".").slice(1).join(".") }))
          .filter((c) => right.includes(c.name))
          .sort((a, b) => score(b.name) - score(a.name));
        if (pairs.length) { j.left = pairs[0].ref; j.right = pairs[0].name; return; }
        j.right = right.find((c) => c.endsWith("_id")) || right[0] || "";
      },
      addJoin() {
        this.spec.joins.push({ table: "", how: "left", left: this.firstColumn(), right: "" });
      },
      trimAfterJoin(i) {
        // Si cambia la tabla de una unión, se quitan referencias a sus columnas antiguas
        const prefix = `t${i + 1}.`;
        const keep = (ref) => !ref || !ref.startsWith(prefix);
        this.spec.filters = this.spec.filters.filter((f) => keep(f.col));
        this.spec.metrics = this.spec.metrics.filter((m) => keep(m.col));
        this.spec.groups = this.spec.groups.filter((g) => keep(g.col));
        this.spec.columns = this.spec.columns.filter(keep);
      },
      removeJoin(i) {
        // Quitar la unión i desplaza los alias de las siguientes: se limpian sus referencias
        const n = this.spec.joins.length;
        for (let k = i; k < n; k++) this.trimAfterJoin(k);
        this.spec.joins.splice(i);
      },
      addFilter() {
        const col = this.firstColumn();
        this.spec.filters.push({ col, op: this.opsFor(col)[0], value: "", value2: "" });
      },
      addMetric() { this.spec.metrics.push({ agg: "count", col: "" }); },
      fixMetric(m) {
        const ok = this.metricColumns(m.agg).some((c) => c.ref === m.col);
        if (m.agg !== "count" && !ok) m.col = (this.metricColumns(m.agg)[0] || {}).ref || "";
      },
      addGroup() {
        const col = this.firstColumn((c) => c.type !== "quantitative") || this.firstColumn();
        this.spec.groups.push({ col, bucket: this.isTemporal(col) ? "month" : "" });
        this.spec.columns = [];
      },
      addSort() {
        const by = (this.compiled.columns[0] || {}).name || "";
        this.spec.sort.push({ by, dir: this.spec.metrics.length ? "desc" : "asc" });
      },

      // ---- servidor
      async compile() {
        if (!this.spec.source) { this.compiled = { sql: "", columns: [], error: "" }; return; }
        const body = new URLSearchParams({ spec: JSON.stringify(this.spec) });
        try {
          const r = await fetch("/builder/compile", { method: "POST", body });
          const data = await r.json();
          this.compiled = { sql: data.sql || "", columns: data.columns || [], error: data.error || "" };
          // Quita órdenes por columnas que ya no existen en la salida
          const names = new Set(this.compiled.columns.map((c) => c.name));
          if (!data.error && this.spec.sort.some((o) => !names.has(o.by))) {
            this.spec.sort = this.spec.sort.filter((o) => names.has(o.by));
          }
        } catch (e) {
          this.compiled.error = "No se pudo contactar con el servidor.";
        }
      },
      async run() {
        if (!this.spec.source) return;
        this.running = true;
        try {
          await htmx.ajax("POST", "/builder/run", {
            target: "#results", swap: "innerHTML",
            values: { spec: JSON.stringify(this.spec), viz: currentViz() },
          });
        } finally {
          this.running = false;
        }
      },
      openAsSql() {
        if (this.compiled.sql) window.location = "/?sql=" + encodeURIComponent(this.compiled.sql);
      },
    }));
  });

  function redrawCharts() {
    document.querySelectorAll("[x-data]").forEach((el) => {
      const d = window.Alpine && Alpine.$data(el);
      if (d && typeof d.draw === "function") d.draw();
    });
  }

  const darkQuery = matchMedia("(prefers-color-scheme: dark)");
  // Sin elección explícita, el tema sigue al sistema: si este cambia, se redibuja
  darkQuery.addEventListener("change", () => {
    if (!document.documentElement.dataset.theme) {
      redrawCharts();
      window.dispatchEvent(new CustomEvent("patobase:theme"));
    }
  });

  const THEME_KEY = "patobase-theme";
  const currentTheme = () =>
    document.documentElement.dataset.theme || (darkQuery.matches ? "dark" : "light");

  document.addEventListener("alpine:init", () => {
    // Botón sol / luna de la barra superior; la elección se guarda en el navegador
    Alpine.data("themeSwitch", () => ({
      theme: currentTheme(),
      init() {
        window.addEventListener("patobase:theme", () => (this.theme = currentTheme()));
      },
      set(t) {
        document.documentElement.dataset.theme = t;
        document.querySelector('meta[name="color-scheme"]')?.setAttribute("content", t);
        try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
        this.theme = t;
        redrawCharts();
      },
      toggle() {
        this.set(this.theme === "dark" ? "light" : "dark");
      },
    }));
  });

  document.addEventListener("DOMContentLoaded", setupEditor);
})();
