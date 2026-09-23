/* 动态产业图画布。
 *
 * 手写 SVG 而不是引图库：这张图是分层固定结构（供给 → 精锡 → 需求），
 * 连线由层次自动生成、用户只拖节点，通用图库的自动布局与路由算法都用不上，
 * 而项目没有构建步骤，多一个几百 KB 的依赖不划算。
 */

const dg = {
  layout: null,       // 当前布局（编辑时就地改）
  values: {},         // node_id → 取值与状态
  pool: [],           // 指标池
  edit: false,
  dirty: false,
  zoom: 1,
  pan: { x: 0, y: 0 },
  active: null,       // 正在绑定的节点 id
};

const DG_STATE = {
  ok:       { stroke: "var(--primary)",    fill: "var(--surface)",     tone: "text-[var(--text)]" },
  stale:    { stroke: "var(--amber)",      fill: "var(--amber-soft)",  tone: "text-[var(--amber)]" },
  blocked:  { stroke: "var(--alert)",      fill: "var(--alert-soft)",  tone: "text-[var(--alert)]" },
  missing_input: { stroke: "var(--amber)", fill: "var(--amber-soft)",  tone: "text-[var(--amber)]", dash: "5 3" },
  no_data:  { stroke: "var(--line)",       fill: "var(--surface)",     tone: "text-[var(--text-muted)]", dash: "4 3" },
  retired:  { stroke: "var(--line)",       fill: "var(--surface)",     tone: "text-[var(--text-muted)]", dash: "4 3" },
  unbound:  { stroke: "var(--line)",       fill: "var(--surface)",     tone: "text-[var(--text-muted)]", dash: "2 4" },
};

const dgEsc = (t) => String(t == null ? "" : t).replace(/[<>&"]/g, (c) =>
  ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));

function dgNum(v, unit) {
  if (v == null) return "—";
  const abs = Math.abs(v);
  const digits = abs >= 1000 ? 0 : abs >= 10 ? 1 : 2;
  return v.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits })
    + (unit ? ` ${unit}` : "");
}

function dgPct(v) {
  if (v == null) return null;
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(1)}%`;
}

/* ---------- 取值 ---------- */

async function dgLoad(templateId) {
  const q = new URLSearchParams();
  if (templateId) q.set("template_id", templateId);
  if (window.DG_DATE) q.set("date", window.DG_DATE);
  const res = await fetch(`/api/sn/diagram/values?${q}`);
  if (!res.ok) return;
  const data = await res.json();
  dg.values = Object.fromEntries(data.nodes.map((n) => [n.node_id, n]));
  const asOf = document.getElementById("dg-asof");
  if (asOf) asOf.textContent = `数据截至 ${data.as_of}`;
  dgRender();
}

/* ---------- 渲染 ---------- */

function dgRender() {
  const nodes = document.getElementById("dg-nodes");
  const edges = document.getElementById("dg-edges");
  const groups = document.getElementById("dg-groups");
  if (!nodes || !dg.layout) return;

  // 分组框画在最底层：它是背景分区，不是节点，也不参与连线
  groups.innerHTML = dg.layout.nodes.filter(dgIsGroup).map((g) => `
    <g class="dg-node dg-group" data-node="${dgEsc(g.id)}" transform="translate(${g.x},${g.y})"
       style="cursor:${dg.edit ? "grab" : "default"}">
      <rect width="${g.w}" height="${g.h}" rx="12" fill="var(--surface)" fill-opacity="0.55"
            stroke="var(--line-strong)" stroke-width="1.4" stroke-dasharray="7 4"/>
      <text x="12" y="20" font-size="12" font-weight="700" fill="var(--text-muted)">${dgEsc(g.label)}</text>
      ${dg.edit ? dgHandle(g) : ""}
    </g>`).join("");

  const by = Object.fromEntries(dg.layout.nodes.map((n) => [n.id, n]));
  edges.innerHTML = (dg.layout.edges || []).map((e) => {
    const a = by[e.from], b = by[e.to];
    if (!a || !b || dgIsGroup(a) || dgIsGroup(b)) return "";
    const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2;
    const mid = (x1 + x2) / 2;
    return `<path d="M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}"
      fill="none" stroke="var(--line-strong)" stroke-width="1.2" opacity="0.55"
      marker-end="url(#dg-arrow)"/>`;
  }).join("");

  nodes.innerHTML = dg.layout.nodes.filter((n) => !dgIsGroup(n)).map((n) => dgNode(n)).join("");
  dgApplyTransform();
}

const dgIsGroup = (n) => n.kind === "group";

function dgHandle(n) {
  // 右下角拖拽把手。只在编辑模式出现，避免看图时误拖。
  return `<rect class="dg-resize" data-resize="${dgEsc(n.id)}"
    x="${n.w - 12}" y="${n.h - 12}" width="12" height="12" rx="3"
    fill="var(--primary)" fill-opacity="0.75" style="cursor:nwse-resize"/>`;
}

function dgDir(v) {
  // 方向色沿用行情条的约定：涨用 --up、跌用 --down。这是事实描述，不是多空判断。
  if (v == null || v === 0) return { cls: "text-[var(--text-muted)]", mark: "", color: null };
  return v > 0
    ? { cls: "text-[var(--up)]", mark: "▲", color: "var(--up)" }
    : { cls: "text-[var(--down)]", mark: "▼", color: "var(--down)" };
}

function dgNode(n) {
  const v = dg.values[n.id] || { state: "unbound", label: n.label };
  const st = DG_STATE[v.state] || DG_STATE.unbound;
  const statics = (n.statics || []).slice(0, 3);
  const proxy = v.proxy;
  // 左侧色条只表示涨跌方向；边框仍然表示数据状态，两者不能混为一谈
  const dirColor = (v.state === "ok" || v.state === "stale")
    ? dgDir(v.delta != null ? v.delta : v.mom).color : null;

  // 值区：阻断与无数据一律不显示数字——显示一个数就等于在说"算出来了"
  let main = "";
  if (v.state === "ok" || v.state === "stale") {
    const d = dgDir(v.delta != null ? v.delta : v.mom);
    const parts = [];
    if (v.delta != null) parts.push(dgNum(v.delta, "").replace(/^-/, "−"));
    if (dgPct(v.mom) != null) parts.push(`${dgPct(v.mom)}`);
    const move = parts.length
      ? `<span class="${d.cls} font-semibold tabular">${d.mark} ${parts.join("　")}</span>` : "";
    const yoy = dgPct(v.yoy) != null
      ? `<span class="${dgDir(v.yoy).cls} tabular">同比 ${dgPct(v.yoy)}</span>` : "";
    // 数值与变化分两行：210px 宽的框里挤一行会把单位甩到下一行去
    main = `
      <div class="text-[15px] font-bold tabular leading-tight truncate">${dgEsc(dgNum(v.value, v.unit))}</div>
      ${move ? `<div class="text-[11px] tabular leading-tight">${move}</div>` : ""}
      <div class="text-[10px] text-[var(--text-muted)] tabular mt-0.5 truncate">
        ${dgEsc(v.frequency)}频 · ${dgEsc(v.as_of || "")}${yoy ? " · " : ""}${yoy}</div>`;
  } else if (v.state === "blocked" || v.state === "missing_input") {
    // 两者都留空，但要说清楚是"补数据就能算"还是"口径对不上，补也没用"
    const title = v.state === "blocked" ? "阻断不出数" : "缺少输入";
    main = `<div class="text-[11px] font-semibold">${title}</div>
            <div class="text-[10px] mt-0.5 leading-snug">${dgEsc((v.note || "").slice(0, 46))}</div>`;
  } else if (v.state === "no_data" || v.state === "retired") {
    main = `<div class="text-[11px] font-semibold">${v.state === "retired" ? "指标已停用" : "尚无数据"}</div>`;
  }
  if (v.state === "stale" && v.note) {
    main += `<div class="text-[10px] mt-0.5">${dgEsc(v.note)}</div>`;
  }

  const staticRows = statics.map((s) =>
    `<div class="text-[10px] text-[var(--text-muted)] truncate">［静］${dgEsc(s.label)} ${dgEsc(s.value)}</div>`
  ).join("");

  return `
  <g class="dg-node" data-node="${dgEsc(n.id)}" transform="translate(${n.x},${n.y})"
     style="cursor:${dg.edit ? "grab" : "default"}">
    <rect width="${n.w}" height="${n.h}" rx="8" fill="${st.fill}" stroke="${st.stroke}"
          stroke-width="${proxy ? 1.4 : 1.6}" ${st.dash ? `stroke-dasharray="${st.dash}"` : ""}/>
    ${proxy ? `<rect width="${n.w}" height="${n.h}" rx="8" fill="url(#dg-hatch)" opacity="0.5"/>` : ""}
    ${dirColor ? `<path d="M 0 8 A 8 8 0 0 1 8 0 L 5 0 L 5 ${n.h} L 8 ${n.h} A 8 8 0 0 1 0 ${n.h - 8} Z"
        fill="${dirColor}" opacity="0.85"/>` : ""}
    ${dg.edit ? dgHandle(n) : ""}
    <foreignObject x="${dirColor ? 15 : 10}" y="8" width="${n.w - (dirColor ? 25 : 20)}" height="${n.h - 16}">
      <div xmlns="http://www.w3.org/1999/xhtml" class="${st.tone}" style="font-family:inherit">
        <div class="text-[11px] font-bold truncate">${dgEsc(n.label)}${proxy ? " ◍" : ""}</div>
        ${main}
        ${staticRows}
      </div>
    </foreignObject>
  </g>`;
}

function dgApplyTransform() {
  const g = document.getElementById("dg-canvas");
  if (!g) return;
  for (const id of ["dg-groups", "dg-edges", "dg-nodes"]) {
    document.getElementById(id).setAttribute(
      "transform", `translate(${dg.pan.x},${dg.pan.y}) scale(${dg.zoom})`);
  }
  const z = document.getElementById("dg-zoom");
  if (z) z.textContent = `${Math.round(dg.zoom * 100)}%`;
}

/* ---------- 缩放与适应 ---------- */

function dgZoom(delta) {
  dg.zoom = Math.min(2, Math.max(0.3, dg.zoom + delta));
  dgApplyTransform();
}

function dgFit() {
  const wrap = document.getElementById("dg-wrap");
  if (!wrap || !dg.layout) return;
  const xs = dg.layout.nodes.map((n) => n.x), ys = dg.layout.nodes.map((n) => n.y);
  const w = Math.max(...dg.layout.nodes.map((n) => n.x + n.w)) - Math.min(...xs);
  const h = Math.max(...dg.layout.nodes.map((n) => n.y + n.h)) - Math.min(...ys);
  const pad = 40;
  dg.zoom = Math.min((wrap.clientWidth - pad) / w, (wrap.clientHeight - pad) / h, 1.4);
  dg.pan = { x: pad / 2 - Math.min(...xs) * dg.zoom, y: pad / 2 - Math.min(...ys) * dg.zoom };
  dgApplyTransform();
}

/* ---------- 编辑：拖动与绑定 ---------- */

function dgToggleEdit() {
  dg.edit = !dg.edit;
  const btn = document.getElementById("dg-edit-btn");
  btn.textContent = dg.edit ? "退出编辑" : "编辑布局";
  btn.classList.toggle("bg-[var(--amber-soft)]", dg.edit);
  btn.classList.toggle("text-[var(--amber)]", dg.edit);
  document.getElementById("dg-hint").classList.toggle("hidden", !dg.edit);
  document.getElementById("dg-tools").classList.toggle("hidden", !dg.edit);
  if (!dg.edit) toggleDrawer("dg-drawer", false);
  dgRender();
}

function dgBindCanvas() {
  const svg = document.getElementById("dg-canvas");
  let drag = null;

  svg.addEventListener("pointerdown", (e) => {
    const handle = e.target.closest?.(".dg-resize");
    const g = e.target.closest?.(".dg-node");
    if (dg.edit && handle) {
      const node = dg.layout.nodes.find((n) => n.id === handle.dataset.resize);
      drag = { node, resize: true, x0: e.clientX, y0: e.clientY, w0: node.w, h0: node.h };
      svg.setPointerCapture(e.pointerId);
    } else if (dg.edit && g) {
      const node = dg.layout.nodes.find((n) => n.id === g.dataset.node);
      drag = { node, x0: e.clientX, y0: e.clientY, nx: node.x, ny: node.y, moved: false };
      svg.setPointerCapture(e.pointerId);
    } else {
      drag = { pan: true, x0: e.clientX, y0: e.clientY, px: dg.pan.x, py: dg.pan.y };
      svg.setPointerCapture(e.pointerId);
    }
  });

  svg.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x0, dy = e.clientY - drag.y0;
    if (drag.pan) {
      dg.pan = { x: drag.px + dx, y: drag.py + dy };
      dgApplyTransform();
      return;
    }
    if (drag.resize) {
      // 下限保证框里还装得下标签与一行数值，不至于被拖成一条缝
      drag.node.w = Math.max(120, Math.round((drag.w0 + dx / dg.zoom) / 10) * 10);
      drag.node.h = Math.max(60, Math.round((drag.h0 + dy / dg.zoom) / 10) * 10);
      dg.dirty = true;
      dgRender();
      return;
    }
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) drag.moved = true;
    drag.node.x = Math.round((drag.nx + dx / dg.zoom) / 10) * 10;  // 对齐到 10px 网格
    drag.node.y = Math.round((drag.ny + dy / dg.zoom) / 10) * 10;
    dg.dirty = true;
    dgRender();
  });

  svg.addEventListener("pointerup", (e) => {
    const wasDrag = drag;
    drag = null;
    svg.releasePointerCapture?.(e.pointerId);
    if (dg.edit && wasDrag && wasDrag.node && !wasDrag.resize && !wasDrag.moved) {
      dgOpenPool(wasDrag.node.id);
    }
    dgSyncSave();
  });

  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    dgZoom(e.deltaY > 0 ? -0.08 : 0.08);
  }, { passive: false });
}

/* ---------- 指标池 ---------- */

async function dgOpenPool(nodeId) {
  dg.active = nodeId;
  const node = dg.layout.nodes.find((n) => n.id === nodeId);
  document.getElementById("dg-node-label").textContent = node.label;
  // 分组框是背景分区，没有指标可绑，只能改名或删除
  const isGroup = dgIsGroup(node);
  document.getElementById("dg-pool").classList.toggle("hidden", isGroup);
  document.getElementById("dg-search").classList.toggle("hidden", isGroup);
  if (isGroup) { toggleDrawer("dg-drawer", true); return; }
  document.getElementById("dg-proxy").checked = !!(node.binding || {}).proxy;
  if (!dg.pool.length) {
    const res = await fetch("/api/sn/export/fields");
    const data = await res.json();
    dg.pool = data.groups.flatMap((g) => g.fields.map((f) => ({ ...f, group: g.name })));
  }
  dgRenderPool();
  toggleDrawer("dg-drawer", true);
  dgSuggest(node);
}

async function dgSuggest(node) {
  const box = document.getElementById("dg-suggest");
  box.innerHTML = '<div class="py-2 text-[var(--text-muted)]">正在匹配…</div>';
  const bound = dg.layout.nodes.map((n) => (n.binding || {}).series_id).filter(Boolean);
  const q = new URLSearchParams({ label: node.label, exclude: bound.join(",") });
  const res = await fetch(`/api/sn/diagram/suggest?${q}`);
  const data = res.ok ? await res.json() : { suggestions: [] };
  if (!data.suggestions.length) {
    // 给不出推荐时明说。那些给不出的节点往往正是数据本来就缺的，这本身就是信息。
    box.innerHTML = `<div class="px-2 py-2 rounded bg-[var(--surface-soft)] text-[var(--text-muted)]">
      按节点名没有匹配到指标 —— 可能是库里确实没有这个环节的数据，请手动搜索确认。</div>`;
    return;
  }
  box.innerHTML = `<div class="text-[10px] font-semibold text-[var(--text-muted)] mb-1">建议参考</div>` +
    data.suggestions.map((f) => `
      <button type="button" onclick="dgBind('${dgEsc(f.series_id)}','observation')"
        class="w-full text-left px-2 py-1.5 rounded hover:bg-[var(--surface-soft)] ${
          f.already_bound ? "opacity-60" : ""}">
        <div class="font-medium truncate">${f.points ? "" : "○ "}${dgEsc(f.name)}${
          f.unit ? `（${dgEsc(f.unit)}）` : ""}${f.already_bound ? " · 本图已绑" : ""}</div>
        <div class="text-[10px] text-[var(--text-muted)] truncate">${dgEsc(f.why)} · ${
          f.points.toLocaleString("zh-CN")} 条 · ${dgEsc(f.frequency)}频</div>
      </button>`).join("");
}

function dgRenderPool() {
  const q = (document.getElementById("dg-search").value || "").trim().toLowerCase();
  const node = dg.layout.nodes.find((n) => n.id === dg.active) || {};
  const bound = (node.binding || {}).series_id;
  // 本图别处已绑的标出来，避免重复绑、也能看出覆盖度
  const usedBy = {};
  for (const n of dg.layout.nodes) {
    const sid = (n.binding || {}).series_id;
    if (sid && n.id !== dg.active) usedBy[sid] = n.label;
  }
  const total = Object.keys(usedBy).length + (bound ? 1 : 0);
  document.getElementById("dg-bound-count").textContent = `本图已绑 ${total} 个指标`;
  const hits = dg.pool
    .filter((f) => f.kind !== "meta")
    .filter((f) => !q || f.label.toLowerCase().includes(q) || f.field.toLowerCase().includes(q))
    .slice(0, 220);
  document.getElementById("dg-pool").innerHTML = hits.length ? hits.map((f) => `
    <button type="button" onclick="dgBind('${dgEsc(f.field)}','${dgEsc(f.kind)}')"
      class="w-full text-left px-2 py-1.5 rounded hover:bg-[var(--surface-soft)] ${
        f.field === bound ? "bg-[var(--primary-soft)]" : ""}">
      <div class="font-medium truncate">${dgEsc(f.label)}${f.unit ? `（${dgEsc(f.unit)}）` : ""}</div>
      <div class="text-[10px] text-[var(--text-muted)] truncate">${dgEsc(f.group)} · ${dgEsc(f.field)}${
        usedBy[f.field] ? ` · <span class="text-[var(--primary)]">已绑于「${dgEsc(usedBy[f.field])}」</span>` : ""}</div>
    </button>`).join("")
    : '<div class="py-6 text-center text-[var(--text-muted)]">没有匹配的指标</div>';
}

function dgBind(field, kind) {
  const node = dg.layout.nodes.find((n) => n.id === dg.active);
  if (!node) return;
  node.binding = kind === "derived"
    ? { kind: "derived", formula_id: field }
    : { kind: "series", series_id: field, proxy: document.getElementById("dg-proxy").checked };
  dg.dirty = true;
  dgSyncSave();
  dgLoad(dg.layout.__id);
  toggleDrawer("dg-drawer", false);
}

function dgUnbind() {
  const node = dg.layout.nodes.find((n) => n.id === dg.active);
  if (!node) return;
  node.binding = { kind: "none" };
  dg.dirty = true;
  dgSyncSave();
  dgLoad(dg.layout.__id);
  toggleDrawer("dg-drawer", false);
}

function dgMarkProxy() {
  const node = dg.layout.nodes.find((n) => n.id === dg.active);
  if (!node || !node.binding || node.binding.kind !== "series") return;
  node.binding.proxy = document.getElementById("dg-proxy").checked;
  dg.dirty = true;
  dgSyncSave();
  dgLoad(dg.layout.__id);
}

/* ---------- 保存 ---------- */

function dgSyncSave() {
  const btn = document.getElementById("dg-save");
  if (btn) btn.disabled = !dg.dirty;
}

async function dgSave() {
  const res = await fetch("/api/sn/diagram/templates", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: dg.layout.__id, name: dg.layout.__name,
                           layout: { nodes: dg.layout.nodes, edges: dg.layout.edges } }),
  });
  const data = await res.json();
  if (!res.ok) { showToast(data.detail || "保存失败"); return; }
  dg.dirty = false;
  dgSyncSave();
  showToast("布局已保存");
}

function dgSwitch(templateId) {
  window.location.href = `/sn/diagram?template=${templateId}`;
}

/* ---------- 启动 ---------- */

function dgInit() {
  const tpl = window.DG_TEMPLATE;
  if (!tpl) return;
  dg.layout = { ...tpl.layout, __id: tpl.id, __name: tpl.name };
  dgBindCanvas();
  dgRender();
  dgFit();
  dgLoad(tpl.id);
}

if (document.getElementById("dg-canvas")) {
  document.addEventListener("DOMContentLoaded", dgInit);
  if (document.readyState !== "loading") dgInit();
}

/* ---------- 增删节点与分组 ---------- */

function dgNextId(prefix) {
  let i = 1;
  while (dg.layout.nodes.some((n) => n.id === `${prefix}${i}`)) i += 1;
  return `${prefix}${i}`;
}

function dgAdd(kind) {
  // 新节点落在当前视口左上角附近，而不是画布原点——否则在远处看不见
  const x = Math.round((-dg.pan.x / dg.zoom + 40) / 10) * 10;
  const y = Math.round((-dg.pan.y / dg.zoom + 40) / 10) * 10;
  const node = kind === "group"
    ? { id: dgNextId("g"), kind: "group", label: "新分组", x, y, w: 420, h: 260, binding: { kind: "none" }, statics: [] }
    : { id: dgNextId("n"), label: "新节点", x, y, w: 210, h: 104, binding: { kind: "none" }, statics: [] };
  dg.layout.nodes.push(node);
  dg.dirty = true;
  dgSyncSave();
  dgRender();
  if (kind !== "group") dgOpenPool(node.id);
}

function dgDeleteActive() {
  const node = dg.layout.nodes.find((n) => n.id === dg.active);
  if (!node) return;
  if (!confirm(`确认删除「${node.label}」吗？与它相连的连线会一并移除。`)) return;
  dg.layout.nodes = dg.layout.nodes.filter((n) => n.id !== node.id);
  dg.layout.edges = (dg.layout.edges || []).filter((e) => e.from !== node.id && e.to !== node.id);
  dg.dirty = true;
  dgSyncSave();
  dgRender();
  toggleDrawer("dg-drawer", false);
}

function dgRenameActive() {
  const node = dg.layout.nodes.find((n) => n.id === dg.active);
  if (!node) return;
  const name = (prompt("节点名称：", node.label) || "").trim();
  if (!name) return;
  node.label = name;
  dg.dirty = true;
  dgSyncSave();
  dgRender();
  const el = document.getElementById("dg-node-label");
  if (el) el.textContent = name;
}

/* ---------- Markdown 导入导出 ---------- */

async function dgOpenMarkdown() {
  const res = await fetch(`/api/sn/diagram/markdown?template_id=${dg.layout.__id}`);
  const data = res.ok ? await res.json() : { markdown: "" };
  document.getElementById("dg-md-text").value = data.markdown || "";
  document.getElementById("dg-md-name").value = "";
  document.getElementById("dg-md-msg").textContent = "";
  toggleDrawer("dg-md-drawer", true);
}

async function dgImportMarkdown() {
  const msg = document.getElementById("dg-md-msg");
  const res = await fetch("/api/sn/diagram/markdown", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ markdown: document.getElementById("dg-md-text").value,
                           name: document.getElementById("dg-md-name").value }),
  });
  const data = await res.json();
  if (!res.ok) { msg.textContent = data.detail || "导入失败"; return; }
  // 导入只给框和标签，绑定要靠「建议参考」逐个补——把这件事说清楚，别让人以为导完就完了
  msg.textContent = `已建「${data.saved.name}」，其中 ${data.unbound} 个节点尚未绑定指标，正在跳转…`;
  setTimeout(() => { window.location.href = `/sn/diagram?template=${data.saved.id}`; }, 900);
}

function dgCopyMarkdown() {
  navigator.clipboard.writeText(document.getElementById("dg-md-text").value).then(
    () => showToast("已复制 Markdown"), () => showToast("复制失败，请手动选择"));
}
