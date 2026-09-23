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
  if (!nodes || !dg.layout) return;

  const by = Object.fromEntries(dg.layout.nodes.map((n) => [n.id, n]));
  edges.innerHTML = (dg.layout.edges || []).map((e) => {
    const a = by[e.from], b = by[e.to];
    if (!a || !b) return "";
    const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2;
    const mid = (x1 + x2) / 2;
    return `<path d="M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}"
      fill="none" stroke="var(--line-strong)" stroke-width="1.2" opacity="0.55"
      marker-end="url(#dg-arrow)"/>`;
  }).join("");

  nodes.innerHTML = dg.layout.nodes.map((n) => dgNode(n)).join("");
  dgApplyTransform();
}

function dgNode(n) {
  const v = dg.values[n.id] || { state: "unbound", label: n.label };
  const st = DG_STATE[v.state] || DG_STATE.unbound;
  const statics = (n.statics || []).slice(0, 3);
  const proxy = v.proxy;

  // 值区：阻断与无数据一律不显示数字——显示一个数就等于在说"算出来了"
  let main = "";
  if (v.state === "ok" || v.state === "stale") {
    const chips = [dgPct(v.mom) && `环比 ${dgPct(v.mom)}`, dgPct(v.yoy) && `同比 ${dgPct(v.yoy)}`]
      .filter(Boolean).join(" · ");
    main = `
      <div class="text-[15px] font-bold tabular leading-tight">${dgEsc(dgNum(v.value, v.unit))}</div>
      <div class="text-[10px] text-[var(--text-muted)] tabular mt-0.5">
        ${dgEsc(v.frequency)}频 · ${dgEsc(v.as_of || "")}${chips ? " · " + dgEsc(chips) : ""}</div>`;
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
    <foreignObject x="10" y="8" width="${n.w - 20}" height="${n.h - 16}">
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
  for (const id of ["dg-nodes", "dg-edges"]) {
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
  if (!dg.edit) toggleDrawer("dg-drawer", false);
  dgRender();
}

function dgBindCanvas() {
  const svg = document.getElementById("dg-canvas");
  let drag = null;

  svg.addEventListener("pointerdown", (e) => {
    const g = e.target.closest?.(".dg-node");
    if (dg.edit && g) {
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
    if (dg.edit && wasDrag && wasDrag.node && !wasDrag.moved) dgOpenPool(wasDrag.node.id);
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
  document.getElementById("dg-proxy").checked = !!(node.binding || {}).proxy;
  if (!dg.pool.length) {
    const res = await fetch("/api/sn/export/fields");
    const data = await res.json();
    dg.pool = data.groups.flatMap((g) => g.fields.map((f) => ({ ...f, group: g.name })));
  }
  dgRenderPool();
  toggleDrawer("dg-drawer", true);
}

function dgRenderPool() {
  const q = (document.getElementById("dg-search").value || "").trim().toLowerCase();
  const node = dg.layout.nodes.find((n) => n.id === dg.active) || {};
  const bound = (node.binding || {}).series_id;
  const hits = dg.pool
    .filter((f) => f.kind !== "meta")
    .filter((f) => !q || f.label.toLowerCase().includes(q) || f.field.toLowerCase().includes(q))
    .slice(0, 220);
  document.getElementById("dg-pool").innerHTML = hits.length ? hits.map((f) => `
    <button type="button" onclick="dgBind('${dgEsc(f.field)}','${dgEsc(f.kind)}')"
      class="w-full text-left px-2 py-1.5 rounded hover:bg-[var(--surface-soft)] ${
        f.field === bound ? "bg-[var(--primary-soft)]" : ""}">
      <div class="font-medium truncate">${dgEsc(f.label)}${f.unit ? `（${dgEsc(f.unit)}）` : ""}</div>
      <div class="text-[10px] text-[var(--text-muted)] truncate">${dgEsc(f.group)} · ${dgEsc(f.field)}</div>
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
