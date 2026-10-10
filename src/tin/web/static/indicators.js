document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("indicator-workspace");
  if (!root) return;
  const get = (id) => document.getElementById(`indicator-${id}`);
  const rows = Array.from(root.querySelectorAll("[data-indicator-row]"));
  const groups = Array.from(root.querySelectorAll("[data-indicator-group]"));
  const names = new Map(rows.map((r) => [r.dataset.seriesId, r.dataset.name]));
  const normalize = (s) => String(s).normalize("NFKC").toLocaleLowerCase();
  const searchText = new Map(rows.map((r) => [r, normalize(r.dataset.search)]));
  const storageKey = "tin.indicators.favorites.v1";
  let favorites = [];
  const selected = new Set();
  let previewController;
  let compareController;
  const storageNotice = (text) => {
    get("storage-note").textContent = text;
    get("storage-note").hidden = false;
  };
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || "[]");
    if (!Array.isArray(saved) || saved.some((sid) => typeof sid !== "string")) throw new Error("invalid");
    favorites = [...new Set(saved)].filter((sid) => names.has(sid)).slice(0, 40);
    if (saved.some((sid) => !names.has(sid))) storageNotice("部分原自选指标已不在清单中，本次未显示。其余自选仍保留。");
  } catch (_) {
    storageNotice("无法读取已保存的自选，请重新选择；若浏览器禁止存储，自选仅在本页有效。");
  }
  let scope = "all";
  const params = new URLSearchParams(location.search);
  get("search").value = params.get("q") || "";
  if (["macro", "industry", "favorites"].includes(params.get("scope"))) scope = params.get("scope");

  function syncScope() {
    root.querySelectorAll("[data-scope]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.scope === scope));
    });
    get("scope-favorite-count").textContent = favorites.length;
    get("search-clear-input").hidden = !get("search").value.trim();
  }

  function filterRows() {
    const terms = normalize(get("search").value).trim().split(/\s+/).filter(Boolean);
    let count = 0;
    rows.forEach((row) => {
      const inScope = scope === "all" || (scope === "macro" && row.dataset.macro === "true") ||
        (scope === "industry" && row.dataset.macro === "false") || (scope === "favorites" && favorites.includes(row.dataset.seriesId));
      row.hidden = !inScope || !terms.every((t) => searchText.get(row).includes(t));
      if (!row.hidden) count++;
    });
    groups.forEach((group) => {
      const visible = Array.from(group.querySelectorAll("[data-indicator-row]")).filter((r) => !r.hidden).length;
      group.hidden = !visible;
      group.querySelector("[data-group-count]").textContent = visible;
      if (terms.length || scope !== "all") group.open = true;
    });
    get("result-count").textContent = `${count} / ${rows.length} 项`;
    get("no-results").hidden = count > 0;
    const url = new URL(location.href);
    if (get("search").value.trim()) url.searchParams.set("q", get("search").value.trim());
    else url.searchParams.delete("q");
    if (scope !== "all") url.searchParams.set("scope", scope);
    else url.searchParams.delete("scope");
    history.replaceState(null, "", url);
    syncScope();
    syncJumpLinks();
    // 日期表单通过原生 submit 提交，同步隐藏字段以保留搜索范围。
    document.querySelectorAll(".date-switcher").forEach((form) => {
      ["q", "scope"].forEach((key) => {
        let input = form.querySelector(`input[name="${key}"]`);
        if (!input) { input = document.createElement("input"); input.type = "hidden"; input.name = key; form.appendChild(input); }
        input.value = url.searchParams.get(key) || "";
      });
      form.querySelectorAll("a").forEach((link) => {
        const target = new URL(link.href);
        ["q", "scope"].forEach((key) => {
          if (url.searchParams.has(key)) target.searchParams.set(key, url.searchParams.get(key));
          else target.searchParams.delete(key);
        });
        link.href = target;
      });
    });
  }

  function syncFavorites() {
    root.querySelectorAll("[data-favorite]").forEach((button) => {
      const sid = button.dataset.favorite;
      const active = favorites.includes(sid);
      button.setAttribute("aria-pressed", String(active));
      button.setAttribute("aria-label", `${active ? "移出" : "加入自选"} ${names.get(sid)}`);
    });
    get("scope-favorite-count").textContent = favorites.length;
    get("favorite-count").textContent = favorites.length;
    get("favorites-empty").hidden = favorites.length > 0;
  }

  function apiParams(ids) {
    const query = new URLSearchParams();
    ids.forEach((sid) => query.append("series_id", sid));
    if (root.dataset.date) query.set("date", root.dataset.date);
    return query;
  }

  async function loadFavorites() {
    if (previewController) previewController.abort();
    previewController = new AbortController();
    const controller = previewController;
    get("favorites-error").hidden = true;
    syncFavorites();
    if (!favorites.length) { get("favorites").replaceChildren(); get("favorites").removeAttribute("aria-busy"); return; }
    get("favorites").setAttribute("aria-busy", "true");
    get("favorites").textContent = "正在读取自选走势…";
    try {
      const response = await fetch(`/api/sn/indicators/previews?${apiParams(favorites)}`, { signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (controller !== previewController) return;
      get("favorites").innerHTML = data.html;
      syncFavorites();
      if (performance.now() - jumpAt < 2000) scrollToJumpTarget();
    } catch (error) {
      if (controller !== previewController || error.name === "AbortError") return;
      get("favorites").replaceChildren();
      get("favorites-error").querySelector("span").textContent = "自选数据暂时无法加载，选择已保留。";
      get("favorites-error").hidden = false;
    } finally {
      if (controller === previewController) get("favorites").removeAttribute("aria-busy");
    }
  }

  let jumpCurrent = "indicator-top";
  let jumpLock = null;
  let revealJumpChip = false;
  let jumpAt = 0;
  function scrollToJumpTarget() {
    const id = jumpLock || (location.hash || "").slice(1);
    const target = document.getElementById(id);
    if (!target) return;
    const margin = id === "indicator-top" ? 68 : stickyOffset();
    const top = Math.max(0, target.getBoundingClientRect().top + window.scrollY - margin);
    const behavior = matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
    window.scrollTo({ top, behavior });
  }
  function stickyOffset() {
    const stack = root.querySelector(".indicator-sticky-stack");
    if (!stack) return 120;
    const top = parseFloat(getComputedStyle(stack).top) || 0;
    const offset = top + stack.offsetHeight + 8;
    root.style.setProperty("--indicator-sticky-offset", `${offset}px`);
    return offset;
  }
  function setJumpActive(targetId) {
    root.querySelectorAll("[data-indicator-jump]").forEach((link) => {
      const active = link.dataset.target === targetId;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "true");
      else link.removeAttribute("aria-current");
    });
    const changed = targetId !== jumpCurrent;
    jumpCurrent = targetId;
    if (jumpLock || (!changed && !revealJumpChip)) return;
    revealJumpChip = false;
    const active = root.querySelector(`[data-indicator-jump][data-target="${CSS.escape(targetId)}"]`);
    const scroller = active?.closest(".macro-filter-scroll");
    if (!active || !scroller) return;
    const delta = active.getBoundingClientRect().left - scroller.getBoundingClientRect().left;
    if (delta < 8) scroller.scrollLeft += delta - 8;
    else if (delta + active.offsetWidth > scroller.clientWidth - 8) {
      scroller.scrollLeft += delta + active.offsetWidth - scroller.clientWidth + 8;
    }
  }
  function updateJumpFromScroll() {
    const line = stickyOffset();
    const atBottom = window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2;
    if (jumpLock) {
      const target = document.getElementById(jumpLock);
      const arrived = jumpLock === "indicator-top"
        ? window.scrollY <= line
        : !target || target.getBoundingClientRect().top <= line + 12 || (atBottom && target === groups.filter((group) => !group.hidden).at(-1));
      if (!arrived) { setJumpActive(jumpLock); return; }
      jumpLock = null;
      revealJumpChip = true;
    }
    let current = "indicator-top";
    const visible = groups.filter((group) => !group.hidden);
    visible.forEach((group) => {
      if (group.getBoundingClientRect().top <= line + 12) current = group.id;
    });
    if (atBottom && visible.length) current = visible[visible.length - 1].id;
    setJumpActive(current);
  }
  function syncJumpLinks() {
    let visible = 0;
    root.querySelectorAll("[data-indicator-jump]").forEach((link) => {
      if (link.dataset.target === "indicator-top") return;
      const group = document.getElementById(link.dataset.target);
      const count = group?.querySelector("[data-group-count]")?.textContent || "0";
      const slot = link.querySelector("[data-jump-count]");
      if (slot) slot.textContent = count;
      link.hidden = !group || group.hidden;
      if (!link.hidden) visible += Number(count) || 0;
    });
    const total = root.querySelector("[data-jump-total]");
    if (total) total.textContent = String(visible);
    updateJumpFromScroll();
  }
  let jumpScheduled = false;
  window.addEventListener("scroll", () => {
    if (jumpScheduled) return;
    jumpScheduled = true;
    requestAnimationFrame(() => { jumpScheduled = false; updateJumpFromScroll(); });
  }, { passive: true });
  window.addEventListener("resize", updateJumpFromScroll);
  window.addEventListener("popstate", () => {
    const id = (location.hash || "#indicator-top").slice(1);
    if (!document.getElementById(id)) return;
    const target = document.getElementById(id);
    if (target.tagName === "DETAILS") target.open = true;
    jumpLock = id;
    jumpAt = performance.now();
    setJumpActive(id);
    scrollToJumpTarget();
  });

  root.addEventListener("click", (event) => {
    const jump = event.target.closest("[data-indicator-jump]");
    if (jump) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
      event.preventDefault();
      const target = document.getElementById(jump.dataset.target);
      if (target?.tagName === "DETAILS") target.open = true;
      jumpLock = jump.dataset.target;
      jumpAt = performance.now();
      setJumpActive(jump.dataset.target);
      history.pushState(null, "", `#${jump.dataset.target}`);
      scrollToJumpTarget();
      return;
    }
    const scopeButton = event.target.closest("[data-scope]");
    if (scopeButton) { scope = scopeButton.dataset.scope; filterRows(); return; }
    const copy = event.target.closest("[data-copy-series]");
    if (copy) { copyText(copy.dataset.copySeries); return; }
    const button = event.target.closest("[data-favorite]");
    if (!button) return;
    const sid = button.dataset.favorite;
    if (favorites.includes(sid)) favorites = favorites.filter((s) => s !== sid);
    else {
      if (favorites.length >= 40) { showToast("最多自选 40 项，请先移出一项"); return; }
      favorites.push(sid);
    }
    try { localStorage.setItem(storageKey, JSON.stringify(favorites)); }
    catch (_) { storageNotice("浏览器未允许保存自选，本次选择仅在当前页面有效。"); }
    filterRows();
    loadFavorites();
  });

  function syncSelection() {
    get("compare-bar").hidden = !selected.size;
    get("selection").textContent = `已选 ${selected.size}/4：${[...selected].map((sid) => names.get(sid)).join("、")}`;
    get("compare-open").disabled = selected.size < 2;
    root.querySelectorAll("[data-compare]").forEach((box) => { box.checked = selected.has(box.dataset.compare); });
    updateJumpFromScroll();
  }
  root.addEventListener("change", (event) => {
    const box = event.target.closest("[data-compare]");
    if (!box) return;
    if (!box.checked) selected.delete(box.dataset.compare);
    else if (selected.size < 4) selected.add(box.dataset.compare);
    else showToast("最多同时比较 4 项指标");
    syncSelection();
  });

  const escape = (value) => String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  function renderComparison(data) {
    const output = get("compare-result");
    if (data.status !== "ok") { output.textContent = data.reason; return; }
    const colors = ["var(--primary)", "#d97706", "#8b5cf6", "#0891b2"];
    const values = data.series.flatMap((s) => s.values);
    const low = Math.min(...values), high = Math.max(...values);
    const span = high - low || 1;
    const times = data.dates.map((d) => Date.parse(d));
    const x = (i) => 65 + (times[i] - times[0]) / (times.at(-1) - times[0]) * 790;
    const y = (v) => 285 - (v - low) / span * 240;
    let svg = '<svg viewBox="0 0 900 330" role="img" aria-label="共同起点为 100 的指标走势对比">';
    for (let i = 0; i <= 4; i++) {
      const value = low + span * i / 4;
      svg += `<path d="M65 ${y(value)} H855" stroke="var(--line)"/><text x="55" y="${y(value) + 4}" text-anchor="end" fill="var(--text-muted)" font-size="11">${value.toFixed(1)}</text>`;
    }
    data.series.forEach((s, index) => {
      svg += `<polyline points="${s.values.map((v, i) => `${x(i)},${y(v)}`).join(" ")}" fill="none" stroke="${colors[index]}" stroke-width="2"/>`;
      s.values.forEach((v, i) => { svg += `<circle class="indicator-compare-point" cx="${x(i)}" cy="${y(v)}" r="4" fill="${colors[index]}"><title>${escape(s.name)} · ${data.dates[i]} · ${v.toFixed(2)}（原值 ${s.raw_values[i]} ${escape(s.unit)}）</title></circle>`; });
    });
    svg += `<text x="65" y="315" fill="var(--text-muted)" font-size="11">${data.dates[0]}</text><text x="855" y="315" text-anchor="end" fill="var(--text-muted)" font-size="11">${data.dates.at(-1)}</text></svg>`;
    const legend = data.series.map((s, i) => `<span style="border-color:${colors[i]}">${escape(s.name)} · 最新原值 ${escape(s.raw_values.at(-1))} ${escape(s.unit)}</span>`).join("");
    output.innerHTML = `<div class="indicator-compare-legend">${legend}</div>${svg}<p class="indicator-muted">${data.dates.length} 个共同观测日；最后共同日期 ${data.dates.at(-1)}。悬停数据点可查看原值。</p>`;
  }

  get("compare-open").addEventListener("click", async () => {
    if (selected.size < 2) return;
    const dialog = get("compare-dialog");
    get("compare-result").textContent = "正在对齐共同观测日…";
    dialog.showModal();
    compareController = new AbortController();
    const controller = compareController;
    try {
      const response = await fetch(`/api/sn/indicators/compare?${apiParams([...selected])}`, { signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (controller === compareController) renderComparison(data);
    } catch (error) {
      if (error.name !== "AbortError") get("compare-result").textContent = "对比暂时无法加载，请关闭后重试。";
    }
  });
  get("compare-close").addEventListener("click", () => get("compare-dialog").close());
  get("compare-dialog").addEventListener("close", () => { if (compareController) compareController.abort(); });
  get("selection-clear").addEventListener("click", () => { selected.clear(); syncSelection(); });
  get("favorites-retry").addEventListener("click", loadFavorites);
  get("search").addEventListener("input", filterRows);
  get("expand").addEventListener("click", () => groups.forEach((g) => { g.open = true; }));
  get("collapse").addEventListener("click", () => groups.forEach((g) => { g.open = false; }));
  get("search-clear").addEventListener("click", () => { get("search").value = ""; scope = "all"; filterRows(); get("search").focus(); });
  get("search-clear-input").addEventListener("click", () => { get("search").value = ""; filterRows(); get("search").focus(); });
  filterRows();
  loadFavorites();
  const initialJump = (location.hash || "").slice(1);
  if (initialJump && root.querySelector(`[data-indicator-jump][data-target="${CSS.escape(initialJump)}"]`)) {
    const target = document.getElementById(initialJump);
    if (target?.tagName === "DETAILS") target.open = true;
    jumpLock = initialJump;
    jumpAt = performance.now();
    scrollToJumpTarget();
  }
});
