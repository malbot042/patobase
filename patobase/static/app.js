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
  document.addEventListener("htmx:configRequest", (evt) => {
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
