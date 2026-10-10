// 页面交互：主题、口径穿透弹层、快速录入抽屉、提示条、合约表与期限结构联动。
// 页面切换走服务端路由，不在前端做 SPA。

(function () {
  const saved = localStorage.getItem("tin-theme");
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  if (saved === "dark" || (saved === null && prefersDark)) document.documentElement.classList.add("dark");
})();

function toggleTheme() {
  const dark = document.documentElement.classList.toggle("dark");
  localStorage.setItem("tin-theme", dark ? "dark" : "light");
}

function togglePopover(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const hidden = el.classList.contains("hidden");
  document.querySelectorAll("[data-popover]").forEach((p) => p.classList.add("hidden"));
  if (hidden) el.classList.remove("hidden");
}

document.addEventListener("click", (e) => {
  if (e.target.closest("[data-popover], [data-popover-trigger]")) return;
  document.querySelectorAll("[data-popover]").forEach((p) => p.classList.add("hidden"));
});

// 抽屉通用开关：支持快速录入、批量导入等多个抽屉共用同一个遮罩
function toggleDrawer(drawerId, open) {
  const drawer = document.getElementById(drawerId);
  const backdrop = document.getElementById("drawer-backdrop");
  if (!drawer) return;
  drawer.classList.toggle("translate-x-full", !open);
  if (open) {
    document.querySelectorAll("[data-drawer]").forEach((d) => {
      if (d.id !== drawerId) d.classList.add("translate-x-full");
    });
  }
  const anyOpen = [...document.querySelectorAll("[data-drawer]")]
    .some((d) => !d.classList.contains("translate-x-full"));
  backdrop.classList.toggle("hidden", !anyOpen);
  // 抽屉打开时锁住主体滚动，避免出现双滚动条
  document.body.classList.toggle("overflow-hidden", anyOpen);
  document.body.classList.toggle("drawer-open", anyOpen);
  if (open && drawerId === "quick-drawer") {
    setTimeout(() => document.getElementById("drawer-value").focus(), 320);
  }
}

function closeDrawers() {
  document.querySelectorAll("[data-drawer]").forEach((d) => d.classList.add("translate-x-full"));
  document.getElementById("drawer-backdrop").classList.add("hidden");
  document.body.classList.remove("overflow-hidden");
}

function openDrawerWith(seriesId) {
  const select = document.getElementById("drawer-series");
  if (select) {
    select.value = seriesId;
    syncDrawerUnit();
  }
  toggleDrawer("quick-drawer", true);
}

function syncDrawerUnit() {
  const select = document.getElementById("drawer-series");
  const unit = document.getElementById("drawer-unit");
  const asOf = document.getElementById("drawer-as-of");
  if (!select || !unit) return;
  const opt = select.selectedOptions[0];
  unit.innerText = opt.dataset.unit || "";
  // 数据时点默认填该指标的常规发布时刻，提醒填的是数据代表时点而非录入时刻
  if (asOf && opt.dataset.asOf && !asOf.dataset.touched) asOf.value = opt.dataset.asOf;
}

function showToast(msg) {
  const toast = document.getElementById("toast");
  const text = document.getElementById("toast-msg");
  if (!toast || !text) return;
  text.innerText = msg;
  toast.classList.remove("hidden");
  setTimeout(() => toast.classList.add("hidden"), 3500);
}

function copyText(txt) {
  navigator.clipboard.writeText(txt).then(
    () => showToast("已复制指标代码：" + txt),
    () => showToast("复制失败，请手动选择"),
  );
}

function highlightContract(contract) {
  const point = document.querySelector('[data-contract-point="' + contract + '"]');
  const label = document.getElementById("chart-hover-indicator");
  document.querySelectorAll("[data-contract-point]").forEach((c) => {
    c.setAttribute("r", c.dataset.main === "1" ? "5.5" : "4");
  });
  if (point) {
    point.setAttribute("r", "7");
    if (label) label.innerText = point.dataset.caption;
  }
}

function resetContract() {
  const label = document.getElementById("chart-hover-indicator");
  document.querySelectorAll("[data-contract-point]").forEach((c) => {
    c.setAttribute("r", c.dataset.main === "1" ? "5.5" : "4");
  });
  const main = document.querySelector('[data-contract-point][data-main="1"]');
  if (label && main) label.innerText = main.dataset.caption;
}

function setMacroFilterActive(targetId) {
  document.querySelectorAll("[data-macro-filter]").forEach((link) => {
    link.classList.toggle("active", link.dataset.target === targetId);
  });
}

function initMacroFilters() {
  const filters = Array.from(document.querySelectorAll("[data-macro-filter]"));
  const sections = Array.from(document.querySelectorAll("section.macro-anchor[id]"));
  if (!filters.length || !sections.length) return;

  filters.forEach((link) => {
    link.addEventListener("click", () => setMacroFilterActive(link.dataset.target));
  });

  let scheduled = false;
  const update = () => {
    scheduled = false;
    const activationLine = 140;
    let current = "macro-top";
    sections.forEach((section) => {
      if (section.getBoundingClientRect().top <= activationLine) current = section.id;
    });
    setMacroFilterActive(current);
  };
  window.addEventListener("scroll", () => {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(update);
  }, { passive: true });
  update();
}

// ---------- 宏观历史图表 ----------

const seriesChartState = {
  data: null,
  range: "1y",
  rendered: [],
  controller: null,
  returnFocus: null,
};

function chartNumber(value, decimals) {
  if (!Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value).replace(/-/g, "−");
}

function chartDate(value) {
  const parts = String(value || "").split("-");
  return parts.length === 3 ? `${parts[0]}/${parts[1]}/${parts[2]}` : value;
}

function chartChange(points, index) {
  if (index <= 0) return { text: "首个观测点", color: "var(--text-muted)" };
  const value = points[index].value;
  const previous = points[index - 1].value;
  const delta = value - previous;
  const sign = delta > 0 ? "+" : "";
  let text;
  if (seriesChartState.data.change_unit === "百分点") {
    text = `较前值 ${sign}${chartNumber(delta, 2)} 个百分点`;
  } else if (seriesChartState.data.raw_unit === "%") {
    text = `较前值 ${sign}${chartNumber(delta * 100, 0)} bp`;
  } else {
    const pct = previous === 0 ? null : delta / Math.abs(previous) * 100;
    text = `较前值 ${sign}${chartNumber(delta, seriesChartState.data.decimals)}`;
    if (pct !== null) text += ` (${pct > 0 ? "+" : ""}${chartNumber(pct, 2)}%)`;
  }
  return {
    text,
    color: delta > 0 ? "var(--up)" : delta < 0 ? "var(--down)" : "var(--text-muted)",
  };
}

function updateSeriesChartReadout(index) {
  const points = seriesChartState.rendered;
  const point = points[index];
  if (!point) return;
  const change = chartChange(points, index);
  document.getElementById("series-chart-readout-date").textContent = chartDate(point.date);
  document.getElementById("series-chart-readout-value").textContent =
    chartNumber(point.value, seriesChartState.data.decimals);
  document.getElementById("series-chart-readout-unit").textContent = seriesChartState.data.unit;
  const changeEl = document.getElementById("series-chart-readout-change");
  changeEl.textContent = change.text;
  changeEl.style.color = change.color;
}

function chartRangePoints() {
  const points = (seriesChartState.data?.points || []).map((point) => ({
    ...point,
    time: Date.parse(`${point.date}T00:00:00Z`),
  })).filter((point) => Number.isFinite(point.value) && Number.isFinite(point.time));
  if (!points.length || seriesChartState.range === "all") return points;
  const days = { "1m": 31, "3m": 93, "1y": 366 }[seriesChartState.range];
  const cutoff = points[points.length - 1].time - days * 86400000;
  const filtered = points.filter((point) => point.time >= cutoff);
  if (filtered.length >= 2 || points.length < 2) return filtered;
  return points.slice(-2);
}

function renderSeriesChart() {
  const points = chartRangePoints();
  seriesChartState.rendered = points;
  const svg = document.getElementById("series-chart-svg");
  if (!points.length) {
    svg.innerHTML = "";
    document.getElementById("series-chart-content").classList.add("hidden");
    const error = document.getElementById("series-chart-error");
    error.textContent = "当前区间没有可用观测";
    error.classList.remove("hidden");
    return;
  }

  const width = 1000;
  const height = 440;
  const margin = { left: 82, right: 24, top: 26, bottom: 46 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const values = points.map((point) => point.value);
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const valueSpan = maximum - minimum;
  const padding = valueSpan === 0 ? Math.max(Math.abs(maximum) * 0.04, 1) : valueSpan * 0.08;
  const low = minimum - padding;
  const high = maximum + padding;
  const firstTime = points[0].time;
  const lastTime = points[points.length - 1].time;
  const timeSpan = Math.max(lastTime - firstTime, 1);
  const xOf = (point) => margin.left + (point.time - firstTime) / timeSpan * plotWidth;
  const yOf = (point) => margin.top + (high - point.value) / (high - low) * plotHeight;

  points.forEach((point) => {
    point.x = points.length === 1 ? margin.left + plotWidth / 2 : xOf(point);
    point.y = yOf(point);
  });

  const line = points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(" ");
  const area = points.length > 1
    ? `${line} L${points[points.length - 1].x.toFixed(2)},${(margin.top + plotHeight).toFixed(2)} `
      + `L${points[0].x.toFixed(2)},${(margin.top + plotHeight).toFixed(2)} Z`
    : "";

  const yTicks = Array.from({ length: 5 }, (_, index) => {
    const ratio = index / 4;
    const y = margin.top + ratio * plotHeight;
    const value = high - ratio * (high - low);
    return `<line x1="${margin.left}" y1="${y}" x2="${width - margin.right}" y2="${y}" stroke="var(--line)" stroke-width="1" vector-effect="non-scaling-stroke"/>`
      + `<text x="${margin.left - 12}" y="${y + 4}" text-anchor="end" fill="var(--text-muted)" font-size="12">${chartNumber(value, seriesChartState.data.decimals)}</text>`;
  }).join("");

  const tickIndexes = [...new Set([0, 0.25, 0.5, 0.75, 1]
    .map((ratio) => Math.round((points.length - 1) * ratio)))];
  const xTicks = tickIndexes.map((index) => {
    const point = points[index];
    return `<line x1="${point.x}" y1="${margin.top}" x2="${point.x}" y2="${margin.top + plotHeight}" stroke="var(--line)" stroke-width="1" stroke-dasharray="3 5" vector-effect="non-scaling-stroke"/>`
      + `<text x="${point.x}" y="${height - 16}" text-anchor="middle" fill="var(--text-muted)" font-size="12">${chartDate(point.date)}</text>`;
  }).join("");

  const latest = points[points.length - 1];
  svg.innerHTML = `
    <defs><clipPath id="series-chart-clip"><rect x="${margin.left}" y="${margin.top}" width="${plotWidth}" height="${plotHeight}"/></clipPath></defs>
    <g>${yTicks}${xTicks}</g>
    <g clip-path="url(#series-chart-clip)">
      ${area ? `<path d="${area}" fill="var(--primary-soft)" opacity="0.72"/>` : ""}
      <path d="${line}" fill="none" stroke="var(--primary)" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
      <circle cx="${latest.x}" cy="${latest.y}" r="4" fill="var(--surface)" stroke="var(--primary)" stroke-width="2" vector-effect="non-scaling-stroke"/>
      <g id="series-chart-crosshair" visibility="hidden">
        <line id="series-chart-cross-x" y1="${margin.top}" y2="${margin.top + plotHeight}" stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>
        <line id="series-chart-cross-y" x1="${margin.left}" x2="${width - margin.right}" stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>
        <circle id="series-chart-cross-point" r="5" fill="var(--surface)" stroke="var(--primary)" stroke-width="2.5" vector-effect="non-scaling-stroke"/>
      </g>
    </g>
    <rect x="${margin.left}" y="${margin.top}" width="${plotWidth}" height="${plotHeight}" fill="transparent"/>
  `;

  svg.dataset.plotLeft = String(margin.left);
  svg.dataset.plotRight = String(width - margin.right);
  svg.onpointermove = moveSeriesChartCrosshair;
  svg.onpointerleave = hideSeriesChartCrosshair;

  document.getElementById("series-chart-stat-latest").textContent =
    `${chartNumber(latest.value, seriesChartState.data.decimals)} ${seriesChartState.data.unit}`;
  document.getElementById("series-chart-stat-high").textContent =
    `${chartNumber(maximum, seriesChartState.data.decimals)} ${seriesChartState.data.unit}`;
  document.getElementById("series-chart-stat-low").textContent =
    `${chartNumber(minimum, seriesChartState.data.decimals)} ${seriesChartState.data.unit}`;
  document.getElementById("series-chart-stat-count").textContent = `${points.length} 条`;
  updateSeriesChartReadout(points.length - 1);
}

function moveSeriesChartCrosshair(event) {
  const svg = event.currentTarget;
  const bounds = svg.getBoundingClientRect();
  const x = (event.clientX - bounds.left) / bounds.width * 1000;
  const left = Number(svg.dataset.plotLeft);
  const right = Number(svg.dataset.plotRight);
  if (x < left || x > right || !seriesChartState.rendered.length) {
    hideSeriesChartCrosshair();
    return;
  }
  let nearest = 0;
  let distance = Infinity;
  seriesChartState.rendered.forEach((point, index) => {
    const next = Math.abs(point.x - x);
    if (next < distance) {
      nearest = index;
      distance = next;
    }
  });
  const point = seriesChartState.rendered[nearest];
  const crosshair = document.getElementById("series-chart-crosshair");
  const xLine = document.getElementById("series-chart-cross-x");
  const yLine = document.getElementById("series-chart-cross-y");
  const dot = document.getElementById("series-chart-cross-point");
  xLine.setAttribute("x1", point.x);
  xLine.setAttribute("x2", point.x);
  yLine.setAttribute("y1", point.y);
  yLine.setAttribute("y2", point.y);
  dot.setAttribute("cx", point.x);
  dot.setAttribute("cy", point.y);
  crosshair.setAttribute("visibility", "visible");
  updateSeriesChartReadout(nearest);
}

function hideSeriesChartCrosshair() {
  const crosshair = document.getElementById("series-chart-crosshair");
  if (crosshair) crosshair.setAttribute("visibility", "hidden");
  if (seriesChartState.rendered.length) updateSeriesChartReadout(seriesChartState.rendered.length - 1);
}

function setSeriesChartRange(range) {
  seriesChartState.range = range;
  document.querySelectorAll("[data-chart-range]").forEach((button) => {
    button.classList.toggle("active", button.dataset.chartRange === range);
  });
  if (seriesChartState.data) renderSeriesChart();
}

async function openSeriesChart(trigger) {
  const modal = document.getElementById("series-chart-modal");
  const seriesId = trigger.dataset.seriesChart;
  const selectedDate = trigger.dataset.chartDate;
  seriesChartState.returnFocus = trigger;
  seriesChartState.data = null;
  setSeriesChartRange("1y");
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("overflow-hidden");
  document.getElementById("series-chart-title").textContent = "历史走势";
  document.getElementById("series-chart-id").textContent = seriesId;
  document.getElementById("series-chart-provider").textContent = "载入中";
  document.getElementById("series-chart-loading").classList.remove("hidden");
  document.getElementById("series-chart-error").classList.add("hidden");
  document.getElementById("series-chart-content").classList.add("hidden");
  document.getElementById("series-chart-source").classList.add("hidden");
  modal.querySelector(".series-chart-close").focus();

  if (seriesChartState.controller) seriesChartState.controller.abort();
  seriesChartState.controller = new AbortController();
  try {
    const params = new URLSearchParams({ limit: "2000" });
    if (selectedDate) params.set("date", selectedDate);
    const response = await fetch(`/api/sn/series/${encodeURIComponent(seriesId)}/history?${params}`, {
      signal: seriesChartState.controller.signal,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "历史数据载入失败");
    seriesChartState.data = data;
    document.getElementById("series-chart-title").textContent = data.name;
    document.getElementById("series-chart-id").textContent = data.series_id;
    document.getElementById("series-chart-provider").textContent = `${data.provider} · ${data.source_level}`;
    const source = document.getElementById("series-chart-source");
    if (data.source_url) {
      source.href = data.source_url;
      source.classList.remove("hidden");
    }
    document.getElementById("series-chart-loading").classList.add("hidden");
    document.getElementById("series-chart-content").classList.remove("hidden");
    renderSeriesChart();
  } catch (error) {
    if (error.name === "AbortError") return;
    document.getElementById("series-chart-loading").classList.add("hidden");
    const errorEl = document.getElementById("series-chart-error");
    errorEl.textContent = error.message;
    errorEl.classList.remove("hidden");
  }
}

function closeSeriesChart() {
  const modal = document.getElementById("series-chart-modal");
  if (!modal || modal.classList.contains("hidden")) return;
  if (seriesChartState.controller) seriesChartState.controller.abort();
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
  const drawerOpen = [...document.querySelectorAll("[data-drawer]")]
    .some((drawer) => !drawer.classList.contains("translate-x-full"));
  if (!drawerOpen) document.body.classList.remove("overflow-hidden");
  if (seriesChartState.returnFocus) seriesChartState.returnFocus.focus();
}

function initSeriesCharts() {
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-series-chart]");
    if (trigger) openSeriesChart(trigger);
  });
  document.querySelectorAll("[data-chart-close]").forEach((button) => {
    button.addEventListener("click", closeSeriesChart);
  });
  document.querySelectorAll("[data-chart-range]").forEach((button) => {
    button.addEventListener("click", () => setSeriesChartRange(button.dataset.chartRange));
  });
}

document.addEventListener("DOMContentLoaded", () => {
  const params = new URLSearchParams(window.location.search);
  if (params.get("msg")) showToast(params.get("msg"));
  const asOf = document.getElementById("drawer-as-of");
  if (asOf) asOf.addEventListener("input", () => { asOf.dataset.touched = "1"; });
  syncDrawerUnit();
  initMacroFilters();
  initSeriesCharts();
  initBriefTimer();
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeSeriesChart();
      closeDrawers();
    }
  });
});

// ---------- 首屏简报评审（F4：采纳 / 改 / 否） ----------

// 阅读计时。§6.3 要靠「采纳耗时分布」识别「秒级采纳 = 没看」这个失败模式，
// 而服务端算不出来：简报可能早上 8 点生成、下午 2 点才提交，但只被看了 3 秒。
// 所以只累计「简报区在视口里 **且** 标签页在前台」的时间——切走标签页立刻停表，
// 否则挂着页面去开会回来一点采纳，会被记成看了两小时，这个指标就废了。
const briefTimer = { seconds: 0, since: null, inView: false, foreground: true };

function briefTimerSync() {
  const running = briefTimer.inView && briefTimer.foreground;
  if (running && briefTimer.since === null) {
    briefTimer.since = Date.now();
  } else if (!running && briefTimer.since !== null) {
    briefTimer.seconds += (Date.now() - briefTimer.since) / 1000;
    briefTimer.since = null;
  }
  briefTimerRender();
}

function briefElapsed() {
  const live = briefTimer.since === null ? 0 : (Date.now() - briefTimer.since) / 1000;
  return Math.max(0, Math.round(briefTimer.seconds + live));
}

function briefTimerRender() {
  const el = document.getElementById("brief-timer");
  if (el) el.innerText = `· 已阅读 ${briefElapsed()} 秒`;
}

function initBriefTimer() {
  const card = document.getElementById("brief-card");
  if (!card) return;
  briefTimer.foreground = document.visibilityState !== "hidden";
  document.addEventListener("visibilitychange", () => {
    briefTimer.foreground = document.visibilityState !== "hidden";
    briefTimerSync();
  });
  if (window.IntersectionObserver) {
    new IntersectionObserver((entries) => {
      briefTimer.inView = entries.some((e) => e.isIntersecting);
      briefTimerSync();
    }, { threshold: 0.15 }).observe(card);
  } else {
    briefTimer.inView = true; // 老浏览器没有 IntersectionObserver，宁可多算也不漏算
  }
  briefTimerSync();
  setInterval(briefTimerRender, 1000);
}

let briefAction = null;

function briefError(message) {
  // 就地标红，不走右下角浮层：浮层会压住这一排主操作按钮（自定义导出上踩过一次）
  const slot = document.getElementById("brief-err");
  if (slot) slot.innerText = message || "";
}

function briefRawClaims() {
  // 回传的必须是库里原样的 claims，不能是页面上补过指标名与来源的那份——
  // 那样一次「改」就把渲染用的装饰写进了证据链。
  const el = document.getElementById("brief-claims");
  if (!el) return [];
  try {
    return JSON.parse(el.textContent);
  } catch (err) {
    return [];
  }
}

function briefOpen(action) {
  briefAction = action;
  briefError("");
  const form = document.getElementById("brief-form");
  const fields = document.getElementById("brief-fields");
  if (!form) return;
  form.classList.remove("hidden");
  // 「否」是整份不要，没有要改的字段；露出编辑区只会让人以为必须填点什么
  if (fields) fields.classList.toggle("hidden", action === "否");
  const submit = document.getElementById("brief-submit");
  if (submit) submit.textContent = action === "否" ? "确认否决" : "提交修改";
  const reason = document.getElementById("brief-reason");
  if (reason) reason.focus();
}

function briefCancel() {
  briefAction = null;
  briefError("");
  const form = document.getElementById("brief-form");
  if (form) form.classList.add("hidden");
}

function briefChanges() {
  const changes = {};
  document.querySelectorAll("#brief-fields [data-field]").forEach((el) => {
    if (el.value === el.dataset.original) return;
    const numeric = el.dataset.field === "range_low" || el.dataset.field === "range_high";
    changes[el.dataset.field] = el.value === "" ? null : numeric ? Number(el.value) : el.value;
  });
  const inputs = [...document.querySelectorAll("#brief-fields [data-claim]")];
  if (inputs.some((el) => el.value !== el.dataset.original)) {
    const raw = briefRawClaims();
    changes.claims = raw.map((c, i) => (inputs[i] ? { ...c, text: inputs[i].value } : c));
  }
  return changes;
}

async function briefPost(payload, button) {
  const card = document.getElementById("brief-card");
  if (!card) return;
  const label = button ? button.textContent : "";
  briefError("");
  if (button) {
    button.disabled = true;
    button.textContent = "提交中…";
  }
  try {
    const res = await fetch(`/api/sn/brief/${card.dataset.briefId}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    // 后端（brief/store.py）的拒绝理由原样呈现：那些话是写给研究员看的，
    // 换成「操作失败」等于把规则说明丢了。
    if (!res.ok) throw new Error(data.detail || "提交失败");
    showToast(`已记录「${payload.action}」（阅读 ${payload.elapsed_seconds} 秒）`);
    setTimeout(() => window.location.reload(), 900);
  } catch (err) {
    briefError(err.message);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = label;
    }
  }
}

function briefReasonText() {
  const el = document.getElementById("brief-reason");
  return el ? el.value.trim() : "";
}

async function briefAdopt() {
  // 采纳不强制写原因：没改就是没意见，逼人编一句只会换回一堆「同意」
  const payload = { action: "采纳", elapsed_seconds: briefElapsed() };
  const reason = briefReasonText();
  if (reason) payload.reason = reason;
  await briefPost(payload, document.getElementById("brief-adopt"));
}

async function briefSubmit() {
  if (!briefAction) return;
  const payload = {
    action: briefAction,
    reason: briefReasonText(),
    elapsed_seconds: briefElapsed(),
  };
  if (briefAction === "改") payload.changes = briefChanges();
  await briefPost(payload, document.getElementById("brief-submit"));
}

// ---------- Excel 批量导入 ----------

let importState = null;

function openImportDrawer() {
  toggleDrawer("import-drawer", true);
}

function importReset() {
  importState = null;
  document.getElementById("import-result").innerHTML = "";
  document.getElementById("import-commit").disabled = true;
  document.getElementById("import-file").value = "";
}

async function importUpload(input) {
  const file = input.files[0];
  if (!file) return;
  const actor = document.getElementById("import-actor").value.trim();
  if (!actor) {
    showToast("请先填写导入操作人");
    input.value = "";
    return;
  }
  const box = document.getElementById("import-result");
  box.innerHTML = '<div class="py-8 text-center text-[var(--text-muted)]">正在解析文件结构与核验口径…</div>';

  const body = new FormData();
  body.append("file", file);
  body.append("actor", actor);
  try {
    const res = await fetch("/api/sn/import/preview", { method: "POST", body });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "解析失败");
    importState = data;
    // SMM 终端导出走摘要确认：按指标Id 精确对齐，没有"这列对应哪个指标"可确认，
    // 几十万行也没人逐行看得完，该判断的是批次级信息
    if (data.mode === "vendor_terminal") smmRenderSummary(data);
    else importRender();
  } catch (err) {
    box.innerHTML = `<div class="p-3 rounded-lg bg-[var(--alert-soft)] text-[var(--alert)]">${err.message}</div>`;
    document.getElementById("import-commit").disabled = true;
  }
}

function smmRenderSummary(d) {
  const esc = (t) => String(t == null ? "" : t).replace(/[<>&"]/g, (c) =>
    ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));
  const n = (v) => Number(v).toLocaleString("zh-CN");
  const stat = (label, value, tone = "") =>
    `<div class="flex items-baseline justify-between py-1">
       <span class="text-[var(--text-muted)]">${label}</span>
       <b class="tabular ${tone}">${value}</b></div>`;

  const notes = (d.caliber_notes || []).concat(d.frequency_mismatches || []);
  const reused = d.reused_indicators || [];
  document.getElementById("import-result").innerHTML = `
    <div class="p-3 rounded-lg bg-[var(--primary-soft)] text-xs">
      <div class="font-semibold text-[var(--primary)] mb-1">识别为${esc(d.vendor ? " " + d.vendor : "数据商终端")}导出格式</div>
      <div class="text-[var(--text-muted)] text-[11px]">按供应商编码精确对齐，不做名称猜测。
        规模超出逐行预览的范围，请确认下面的批次信息。</div>
    </div>
    <div class="mt-3 text-xs">
      ${stat("新登记指标", n(d.new_indicators) + " 个")}
      ${stat("复用已登记", n(reused.length) + " 个")}
      ${reused.length ? `<div class="pl-3 text-[11px] text-[var(--text-muted)]">${
        reused.map((r) => esc(r.name) + " — " + esc(r.series_id)).join("；")}</div>` : ""}
      ${stat("跳过已停用列", n(d.skipped_discontinued) + " 列")}
      ${stat("观测点", n(d.points) + " 条")}
      ${stat("时间范围", esc(d.first_date) + " ~ " + esc(d.last_date))}
      ${d.future_points ? stat("其中晚于今天的点", n(d.future_points) + " 条（年/季频按期末标注，该期尚未走完）",
                               "text-[var(--amber)]") : ""}
      ${stat("登记与推导的差异", notes.length ? n(notes.length) + " 条（以登记为准）" : "无",
             notes.length ? "text-[var(--amber)]" : "")}
      ${notes.length ? `<div class="pl-3 text-[11px] text-[var(--amber)]">${
        notes.slice(0, 6).map(esc).join("<br>")}</div>` : ""}
      <div class="mt-2 pt-2 border-t border-[var(--line)]">
        <div class="text-[var(--text-muted)] mb-1">新指标分类</div>
        <div class="flex flex-wrap gap-1">${Object.entries(d.categories || {}).map(([k, v]) =>
          `<span class="px-1.5 py-0.5 rounded bg-[var(--surface-soft)]">${esc(k)} ${v}</span>`).join("")}</div>
      </div>
    </div>
    <div class="mt-3 p-2.5 rounded-md bg-[var(--amber-soft)] text-[var(--amber)] text-[11px]">
      新指标的口径由系统从 SMM 的层级命名推导，推不出的维度留空。导入后请在「指标」页复核口径。
    </div>
    <div class="mt-3 flex justify-end">
      <button type="button" onclick="smmCommit()" id="smm-commit"
        class="px-5 py-2 rounded-md bg-[var(--primary)] text-white text-xs font-semibold hover:bg-[var(--primary-hover)] disabled:opacity-40">
        确认导入 ${n(d.points)} 条</button>
    </div>`;
  const commit = document.getElementById("import-commit");
  if (commit) commit.classList.add("hidden");  // 这条路不走逐行确认的提交按钮
}

async function smmCommit() {
  const btn = document.getElementById("smm-commit");
  btn.disabled = true;
  btn.textContent = "正在提交…";
  try {
    const res = await fetch("/api/sn/import/terminal/commit", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: importState.token, filename: importState.filename,
                             actor: document.getElementById("import-actor").value.trim() }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "提交失败");
    smmPoll(data.job_id);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "确认导入";
    showToast(err.message);
  }
}

async function smmPoll(jobId) {
  const box = document.getElementById("import-result");
  const n = (v) => Number(v).toLocaleString("zh-CN");
  const tick = async () => {
    const res = await fetch(`/api/sn/import/jobs/${jobId}`);
    if (!res.ok) {
      box.innerHTML = '<div class="p-3 rounded-lg bg-[var(--alert-soft)] text-[var(--alert)]">任务已过期，请重新上传</div>';
      return;
    }
    const j = await res.json();
    if (j.state === "running") {
      box.innerHTML = `
        <div class="py-6 text-center text-xs">
          <div class="font-semibold">正在入库…</div>
          <div class="mt-3 h-2 rounded-full bg-[var(--surface-soft)] overflow-hidden">
            <div class="h-full bg-[var(--primary)] transition-all" style="width:${j.percent}%"></div></div>
          <div class="mt-2 text-[var(--text-muted)] tabular">
            ${j.done}/${j.total} 条序列 · 已写入 ${n(j.written)} 条</div>
          <div class="mt-1 text-[11px] text-[var(--text-muted)]">几十万条需要几分钟，可以离开本页，回来再看进度</div>
        </div>`;
      setTimeout(tick, 2000);
      return;
    }
    if (j.state === "failed") {
      box.innerHTML = `<div class="p-3 rounded-lg bg-[var(--alert-soft)] text-[var(--alert)] text-xs">
        导入失败：${j.error}</div>`;
      return;
    }
    box.innerHTML = `
      <div class="p-3 rounded-lg bg-[var(--primary-soft)] text-xs">
        <div class="font-semibold text-[var(--primary)]">导入完成</div>
        <div class="mt-2 tabular">新登记指标 ${n(j.registered)} 个 ·
          写入 ${n(j.written)} 条 · 值未变跳过 ${n(j.unchanged)} 条</div>
        ${j.rejected.length ? `<div class="mt-2 text-[var(--alert)]">
          ${j.rejected.length} 条被闸门拒绝：<br>${j.rejected.slice(0, 5).join("<br>")}</div>` : ""}
      </div>`;
    showToast(`已导入 ${n(j.written)} 条观测`);
  };
  tick();
}

function importRender() {
  const d = importState;
  const options = JSON.parse(document.getElementById("import-options").textContent);
  const esc = (t) => String(t == null ? "" : t).replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
  const pending = d.columns.filter((c) => c.needs_choice);

  const chip = (label, n, tone) =>
    `<span class="px-2 py-1 rounded ${tone}">${label} <b class="tabular">${n}</b></span>`;
  let html = `<div class="flex flex-wrap gap-2 text-[11px] mb-3">
    ${chip("可导入", d.ready_count, "bg-[var(--primary-soft)] text-[var(--primary)]")}
    ${chip("将产生修订", d.revision_count, "bg-[var(--amber-soft)] text-[var(--amber)]")}
    ${chip("相同忽略", d.skipped_count, "bg-[var(--surface-soft)] text-[var(--text-muted)]")}
    ${chip("异常未导入", d.error_count, "bg-[var(--alert-soft)] text-[var(--alert)]")}
  </div>`;

  if (pending.length) {
    html += `<div class="mb-3 p-3 rounded-lg bg-[var(--amber-soft)] text-[11px]">
      <b class="text-[var(--amber)]">${pending.length} 列需要确认口径后才能导入</b>
      <div class="text-[var(--text-muted)] mt-0.5">名称匹配无法区分品位与计量口径，请逐列选定。</div>
      ${pending.map((c) => `<div class="mt-2">
        <div class="font-semibold">${esc(c.header)}</div>
        <div class="text-[var(--text-muted)] mb-1">${esc(c.error)}</div>
        <select data-col="${c.col_key}" onchange="importSyncCommit()" class="w-full px-2 py-1.5 rounded-md">
          <option value="">— 请选择对应指标 —</option>
          ${options.map((o) => `<option value="${o.series_id}">${esc(o.name)}（${esc(o.unit)}）— ${o.series_id}</option>`).join("")}
        </select></div>`).join("")}
    </div>`;
  }

  const list = (title, rows, tone, checked, selectable) => {
    if (!rows.length) return "";
    return `<details class="mb-2" ${rows.length && selectable ? "open" : ""}>
      <summary class="cursor-pointer text-xs font-semibold ${tone}">${title}（${rows.length}）</summary>
      <div class="mt-1 max-h-52 overflow-y-auto text-[11px] divide-y divide-[var(--line)]">
        ${rows.map((r) => `<label class="flex items-start gap-2 py-1.5">
          ${selectable ? `<input type="checkbox" class="mt-0.5" data-row="${r.row_key}" ${checked ? "checked" : ""}>` : ""}
          <span class="flex-1">
            <span class="tabular">${esc(r.as_of).slice(0, 16).replace("T", " ")}</span>
            <span class="ml-2">${esc(r.series_id || "—")}</span>
            <b class="ml-2 tabular">${r.value}</b>
            ${r.old_value != null ? `<span class="ml-2 text-[var(--amber)]">原值 ${r.old_value} → 新值 ${r.value}</span>` : ""}
            ${r.warn ? `<div class="text-[var(--amber)]">${esc(r.warn)}</div>` : ""}
            ${r.reason ? `<div class="text-[var(--alert)]">${esc(r.reason)}</div>` : ""}
            ${r.time_filled ? '<span class="ml-1 text-[var(--text-muted)]">[时点按登记发布时刻补齐]</span>' : ""}
          </span></label>`).join("")}
      </div></details>`;
  };

  const by = (c) => d.rows.filter((r) => r.category === c);
  html += list("可导入", by("ready"), "text-[var(--primary)]", true, true);
  html += list("将产生修订（需逐条确认）", by("revision"), "text-[var(--amber)]", false, true);
  html += list("异常未导入", by("error"), "text-[var(--alert)]", false, false);
  if (d.skipped_count) {
    html += `<div class="text-[11px] text-[var(--text-muted)]">${d.skipped_count} 条与库内数值完全一致，已自动忽略。</div>`;
  }
  document.getElementById("import-result").innerHTML = html;
  importSyncCommit();
}

function importSyncCommit() {
  const unresolved = [...document.querySelectorAll("#import-result select[data-col]")]
    .some((s) => !s.value);
  const chosen = document.querySelectorAll("#import-result input[data-row]:checked").length;
  const btn = document.getElementById("import-commit");
  btn.disabled = unresolved || !chosen;
  btn.textContent = unresolved ? "请先确认口径" : `确认入库（${chosen} 条）`;
}

async function importCommit() {
  const note = document.getElementById("import-note").value.trim();
  const actor = document.getElementById("import-actor").value.trim();
  if (!note || !actor) {
    showToast("导入操作人与来源说明均为必填");
    return;
  }
  const overrides = {};
  document.querySelectorAll("#import-result select[data-col]").forEach((s) => {
    if (s.value) overrides[s.dataset.col] = { series_id: s.value };
  });
  const keys = [...document.querySelectorAll("#import-result input[data-row]:checked")]
    .map((c) => c.dataset.row);

  const btn = document.getElementById("import-commit");
  btn.disabled = true;
  btn.textContent = "正在入库…";
  try {
    const res = await fetch("/api/sn/import/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        preview_id: importState.preview_id, selected_row_keys: keys,
        column_overrides: overrides, actor, default_note: note,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "入库失败");
    const stale = data.stale_rows.length ? `，${data.stale_rows.length} 条被拦截` : "";
    showToast(`已入库 ${data.committed} 条${stale}；已重算 ${data.recalculated_dates.length} 个交易日`);
    setTimeout(() => window.location.reload(), 1200);
  } catch (err) {
    showToast(err.message);
    importSyncCommit();
  }
}

document.addEventListener("change", (e) => {
  if (e.target.matches("#import-result input[data-row]")) importSyncCommit();
});

// ---------- Excel 自定义导出 ----------

const exportState = { groups: [], templates: [], selected: [], templateId: null, pristine: "[]" };

async function openExportDrawer() {
  toggleDrawer("export-drawer", true);
  exportClearErrors();  // 上次的报错不该跟到下一次打开
  if (!exportState.groups.length) {
    const [fields, templates] = await Promise.all([
      fetch("/api/sn/export/fields").then((r) => r.json()),
      fetch("/api/sn/export/templates").then((r) => r.json()),
    ]);
    exportState.groups = fields.groups;
    exportState.templates = templates.templates;
    const preset = exportState.templates.find((t) => t.is_default) || exportState.templates[0];
    exportApplyTemplate(preset ? String(preset.id) : "");
  }
}

function exportApplyTemplate(id) {
  const tpl = exportState.templates.find((t) => String(t.id) === String(id));
  exportState.templateId = tpl ? tpl.id : null;
  exportState.selected = tpl
    ? tpl.columns.map((c) => ({ ...c }))
    : [{ field: "trade_date", label: "交易日", kind: "meta" }];
  exportState.pristine = JSON.stringify(exportState.selected);
  if (tpl) {
    document.getElementById("export-range").value = tpl.date_range;
    document.getElementById("export-sort").value = tpl.sort_order;
  }
  exportRender();
}

function exportIsDirty() {
  return JSON.stringify(exportState.selected) !== exportState.pristine;
}

function exportToggleField(field, label, kind) {
  const at = exportState.selected.findIndex((c) => c.field === field);
  if (at >= 0) exportState.selected.splice(at, 1);
  else exportState.selected.push({ field, label, kind });
  exportRender();
}

function exportMove(index, delta) {
  const list = exportState.selected;
  const to = index + delta;
  if (to < 0 || to >= list.length) return;
  [list[index], list[to]] = [list[to], list[index]];
  exportState.activeField = list[to].field;
  exportRender();
}

function exportUndo() {
  exportState.selected = JSON.parse(exportState.pristine);
  exportRender();
  showToast("已恢复到模板初始配置");
}

function exportSelectAll(on) {
  const keep = exportState.selected.filter((c) => c.field === "trade_date");
  exportState.selected = on
    ? exportState.groups.flatMap((g) => g.fields.map((f) => ({ field: f.field, label: f.label, kind: f.kind })))
    : keep;
  exportRender();
}

function exportToggleGroup(groupIndex, on) {
  const group = exportState.groups[groupIndex];
  const inGroup = new Set(group.fields.map((f) => f.field));
  exportState.selected = exportState.selected.filter((c) => !inGroup.has(c.field) || c.field === "trade_date");
  if (on) {
    group.fields.forEach((f) => {
      if (!exportState.selected.some((c) => c.field === f.field)) {
        exportState.selected.push({ field: f.field, label: f.label, kind: f.kind });
      }
    });
  }
  exportRender();
}

function exportSetActive(field, event) {
  // 胶囊内的 ◀ ▶ × 各有自己的动作，点它们不该顺带改选中态
  if (event && event.target.closest("button")) return;
  exportState.activeField = exportState.activeField === field ? null : field;
  exportRender();
}

let exportDragFrom = null;

function exportDragStart(index, event) {
  exportDragFrom = index;
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", String(index));
}

function exportDragOver(index, event) {
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
}

function exportDrop(index, event) {
  event.preventDefault();
  const from = exportDragFrom ?? Number(event.dataTransfer.getData("text/plain"));
  exportDragFrom = null;
  if (from === null || Number.isNaN(from) || from === index) return;
  const list = exportState.selected;
  const [moved] = list.splice(from, 1);
  list.splice(index, 0, moved);
  exportState.activeField = moved.field;
  exportRender();
}

function exportRender() {
  const esc = (t) => String(t == null ? "" : t).replace(/[<>&"]/g, (c) =>
    ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));
  const dirty = exportIsDirty();
  const tpl = exportState.templates.find((t) => t.id === exportState.templateId);

  const picker = document.getElementById("export-template-bar");
  picker.innerHTML = `
    <select id="export-template" onchange="exportApplyTemplate(this.value)" class="flex-1 min-w-0 px-2 py-1.5 rounded-md text-xs">
      ${exportState.templates.length
        ? exportState.templates.map((t) => `<option value="${t.id}" ${t.id === exportState.templateId ? "selected" : ""}>
            ${esc(t.name)}${t.is_default ? "（默认）" : ""}${t.id === exportState.templateId && dirty ? " · 已修改" : ""}</option>`).join("")
        : '<option value="">未命名配置（未保存）</option>'}
    </select>
    ${dirty ? '<button type="button" onclick="exportUndo()" class="px-2 py-1.5 text-xs rounded-md border border-[var(--line)] hover:bg-[var(--surface-soft)]">撤销修改</button>' : ""}
    <button type="button" onclick="exportSave(false)" ${dirty && tpl ? "" : "disabled"}
      class="px-2 py-1.5 text-xs rounded-md font-semibold ${dirty && tpl ? "bg-[var(--primary)] text-white hover:bg-[var(--primary-hover)]" : "border border-[var(--line)] text-[var(--text-muted)] opacity-50"}">覆盖保存</button>
    <button type="button" onclick="exportSave(true)" ${exportState.selected.length ? "" : "disabled"}
      title="${exportState.selected.length ? "" : "请先勾选指标字段后再保存为模板"}"
      class="px-2 py-1.5 text-xs rounded-md border border-[var(--line)] hover:bg-[var(--surface-soft)] disabled:opacity-40">另存为</button>
    ${tpl ? `<button type="button" onclick="exportDelete()" class="px-2 py-1.5 text-xs rounded-md text-[var(--alert)] hover:bg-[var(--alert-soft)]">删除</button>` : ""}`;

  document.getElementById("export-pills").innerHTML = exportState.selected.map((c, i) => `
    <span draggable="true" title="点击选中后用 ◀ ▶ 调序，也可直接拖拽"
          data-pill="${c.field}" ${c.field === exportState.activeField ? 'data-active="1"' : ""}
          onclick="exportSetActive('${c.field}', event)"
          ondragstart="exportDragStart(${i}, event)" ondragover="exportDragOver(${i}, event)"
          ondrop="exportDrop(${i}, event)"
          class="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] cursor-grab active:cursor-grabbing ${
            c.field === exportState.activeField
              ? "bg-[var(--primary)] text-white ring-2 ring-[var(--primary)] ring-offset-1"
              : "bg-[var(--primary-soft)] text-[var(--primary)]"}">
      <span class="opacity-50">⠿</span><b class="tabular">${i + 1}.</b>${esc(c.label)}
      ${c.field === "trade_date" ? "" : `
        <button type="button" onclick="exportMove(${i}, -1)" ${i === 0 ? "disabled" : ""} class="disabled:opacity-30" title="前移">◀</button>
        <button type="button" onclick="exportMove(${i}, 1)" ${i === exportState.selected.length - 1 ? "disabled" : ""} class="disabled:opacity-30" title="后移">▶</button>
        <button type="button" onclick="exportToggleField('${c.field}')" title="移除">×</button>`}
    </span>`).join("") || '<span class="text-[11px] text-[var(--text-muted)]">尚未选择字段</span>';

  const chosen = new Set(exportState.selected.map((c) => c.field));
  document.getElementById("export-groups").innerHTML = exportState.groups.map((g, gi) => {
    const picked = g.fields.filter((f) => chosen.has(f.field)).length;
    const allOn = picked === g.fields.length;
    return `<details class="border-b border-[var(--line)] py-1.5" ${gi < 2 ? "open" : ""}>
      <summary class="cursor-pointer text-xs font-semibold flex items-center gap-1.5">
        <input type="checkbox" onclick="event.preventDefault(); exportToggleGroup(${gi}, ${!allOn})"
               ${allOn ? "checked" : ""} ${picked && !allOn ? "data-partial" : ""}
               title="${allOn ? "清空本组" : "全选本组"}">
        <span class="chev text-[var(--text-muted)]">▸</span><span>${esc(g.name)}</span>
        <span class="text-[var(--text-muted)] font-normal tabular">(${picked}/${g.fields.length})</span></summary>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-1 mt-1.5">
        ${g.fields.map((f) => `<label class="flex items-start gap-1.5 text-[11px] py-0.5">
          <input type="checkbox" class="mt-0.5" ${chosen.has(f.field) ? "checked" : ""}
                 onchange="exportToggleField('${f.field}', '${esc(f.label)}', '${f.kind}')">
          <span><span class="font-medium">${esc(f.label)}</span>
            ${f.unit ? `<span class="text-[var(--text-muted)]">（${esc(f.unit)}）</span>` : ""}
            ${f.note ? `<div class="text-[var(--text-muted)]">${esc(f.note)}</div>` : ""}</span></label>`).join("")}
      </div></details>`;
  }).join("");

  document.getElementById("export-custom-dates").classList.toggle(
    "hidden", document.getElementById("export-range").value !== "custom");
  document.querySelectorAll("#export-groups input[data-partial]").forEach((box) => {
    box.indeterminate = true;  // 半选：本组只勾了一部分
  });
}

function exportPayload() {
  return {
    columns: exportState.selected,
    date_range: document.getElementById("export-range").value,
    sort_order: document.getElementById("export-sort").value,
    start_date: document.getElementById("export-start").value || null,
    end_date: document.getElementById("export-end").value || null,
    actor: document.getElementById("export-actor").value.trim(),
  };
}

async function exportSave(asNew) {
  const tpl = exportState.templates.find((t) => t.id === exportState.templateId);
  let name = tpl ? tpl.name : "";
  if (asNew) {
    name = (prompt("新模板名称：", name ? `${name} 副本` : "晨报核心") || "").trim();
    if (!name) return;
  } else if (!confirm(`确认将当前列配置覆盖保存至模板「${name}」吗？原模板配置将被替换。`)) {
    return;
  }
  const body = { ...exportPayload(), name, id: asNew ? null : exportState.templateId };
  const res = await fetch("/api/sn/export/templates", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) {
    showToast(data.detail || "保存失败");
    return;
  }
  exportState.templates = data.templates;
  exportState.templateId = data.saved.id;
  exportState.pristine = JSON.stringify(exportState.selected);
  exportRender();
  showToast(asNew ? `已另存为「${name}」` : `已覆盖保存「${name}」`);
}

async function exportDelete() {
  const tpl = exportState.templates.find((t) => t.id === exportState.templateId);
  if (!tpl || !confirm(`确认删除导出模板「${tpl.name}」吗？此操作不可逆。`)) return;
  const res = await fetch(`/api/sn/export/templates/${tpl.id}`, { method: "DELETE" });
  const data = await res.json();
  if (!res.ok) {
    showToast(data.detail || "删除失败");
    return;
  }
  exportState.templates = data.templates;
  const next = exportState.templates.find((t) => t.is_default) || exportState.templates[0];
  exportApplyTemplate(next ? String(next.id) : "");
  showToast(`已删除模板「${tpl.name}」`);
}

function exportFieldError(inputIds, slotId, message) {
  const slot = document.getElementById(slotId);
  if (slot) slot.textContent = message || "";
  inputIds.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.toggle("field-error", Boolean(message));
    el.setAttribute("aria-invalid", message ? "true" : "false");
  });
}

function exportClearErrors() {
  exportFieldError(["export-actor"], "export-actor-err", "");
  exportFieldError(["export-start", "export-end"], "export-dates-err", "");
}

function exportValidate() {
  const payload = exportPayload();
  exportClearErrors();
  if (!payload.actor) {
    exportFieldError(["export-actor"], "export-actor-err", "请填写导出人姓名，导出记录要留痕到人");
    document.getElementById("export-actor").focus();
    return null;
  }
  if (payload.date_range === "custom" && !(payload.start_date && payload.end_date)) {
    const missing = payload.start_date ? "export-end" : "export-start";
    exportFieldError(["export-start", "export-end"], "export-dates-err", "自定义区间需要同时填写起止日期");
    document.getElementById(missing).focus();
    return null;
  }
  if (payload.date_range === "custom" && payload.start_date > payload.end_date) {
    exportFieldError(["export-start", "export-end"], "export-dates-err", "起始日期不能晚于结束日期");
    document.getElementById("export-start").focus();
    return null;
  }
  return payload;
}

async function exportRun() {
  const payload = exportValidate();
  if (!payload) return;
  const btn = document.getElementById("export-run");
  btn.disabled = true;
  btn.textContent = "正在生成…";
  try {
    const res = await fetch("/api/sn/export/excel", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error((await res.json()).detail || "导出失败");
    const blob = await res.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = decodeURIComponent((res.headers.get("Content-Disposition") || "").split("filename=")[1] || "").replace(/"/g, "") || "导出.xlsx";
    link.click();
    URL.revokeObjectURL(link.href);
    const skipped = decodeURIComponent(res.headers.get("X-Export-Skipped") || "");
    showToast(`已导出 ${res.headers.get("X-Export-Rows")} 行${skipped ? `；${skipped} 已停用，本次略过` : ""}`);
  } catch (err) {
    showToast(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "导出 Excel (.xlsx)";
  }
}
