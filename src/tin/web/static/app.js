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

document.addEventListener("DOMContentLoaded", () => {
  const params = new URLSearchParams(window.location.search);
  if (params.get("msg")) showToast(params.get("msg"));
  const asOf = document.getElementById("drawer-as-of");
  if (asOf) asOf.addEventListener("input", () => { asOf.dataset.touched = "1"; });
  syncDrawerUnit();
  initMacroFilters();
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawers();
  });
});

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
    importRender();
  } catch (err) {
    box.innerHTML = `<div class="p-3 rounded-lg bg-[var(--alert-soft)] text-[var(--alert)]">${err.message}</div>`;
    document.getElementById("import-commit").disabled = true;
  }
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
