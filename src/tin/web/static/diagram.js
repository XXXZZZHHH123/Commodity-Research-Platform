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

/* 分组框按嵌套深度换配色。
 *
 * 刻意避开琥珀与红——那两个颜色已经被节点状态（断更 / 阻断）占着，分组再用就分不清
 * 「这框是二级分组」还是「这里出问题了」。留下的中性灰 → 蓝 → 主题青是纯结构信号。
 * 光靠字号和透明度差一点点是看不出层级的，所以边框色、底色、标签色块三者一起变。 */
const DG_DEPTH = [
  { stroke: "var(--line-strong)", fill: "var(--surface)",      fillOpacity: 0.55,
    width: 2.2, dash: "10 5", pill: 0.20, label: "var(--text)" },
  { stroke: "var(--blue)",        fill: "var(--blue-soft)",    fillOpacity: 0.75,
    width: 1.8, dash: "6 4",  pill: 0.18, label: "var(--blue)" },
  { stroke: "var(--primary)",     fill: "var(--primary-soft)", fillOpacity: 0.85,
    width: 1.5, dash: "4 3",  pill: 0.18, label: "var(--primary)" },
];

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

async function dgLoad() {
  // 按**前端此刻的布局**取值，而不是按 template_id 让服务端去库里读。
  // 新增的节点、刚改的绑定在保存前库里并不存在，走 template_id 就永远查不到值——
  // 表现出来就是「绑了指标但方框不显示数据」。
  //
  // 但这一条请求失败不能把整张图清空。之前失败即 return，dg.values 停在空对象，
  // 于是**每个方框都渲染成「未绑定」**——看上去像是绑定全丢了，实际只是取值没回来。
  // 服务端没重启（POST 这个路由是新加的，旧进程上是 405）就会正好撞上。
  if (!dg.layout) return;
  const body = JSON.stringify({ date: window.DG_DATE || null,
                                layout: { nodes: dg.layout.nodes, edges: dg.layout.edges } });
  let data = null, why = "";
  try {
    const res = await fetch("/api/sn/diagram/values", {
      method: "POST", headers: { "Content-Type": "application/json" }, body });
    if (res.ok) data = await res.json();
    else why = `HTTP ${res.status}`;
  } catch (e) { why = e.message; }

  if (!data) {
    // 退回按已保存的布局取值：拿不到未保存的改动，但至少不是一张全灰的图
    try {
      const res = await fetch(`/api/sn/diagram/values?template_id=${dg.layout.__id}${
        window.DG_DATE ? `&date=${window.DG_DATE}` : ""}`);
      if (res.ok) {
        data = await res.json();
        // 405 只有一个原因：这个路由是新加的，服务端进程还是旧代码。
        // 静态文件每次请求都从磁盘读，Python 代码是进程启动时加载的——所以会出现
        // 「前端是新的、后端是旧的」，前端功能看着都在，一调新接口就失败。
        showToast(why === "HTTP 405"
          ? "服务端还在跑旧代码（新接口返回 405）—— 请重启 uvicorn。现在显示的是已保存版本，新增/改绑的节点不会出数"
          : `实时取值不可用（${why}），显示的是已保存版本`);
      }
    } catch (e) { /* 两条都不通，下面统一报 */ }
  }
  if (!data) { showToast(`取值失败（${why}）—— 方框显示的不是真实状态`); return; }

  dg.values = Object.fromEntries(data.nodes.map((n) => [n.node_id, n]));
  dg.judgment = data.judgment || {};
  const asOf = document.getElementById("dg-asof");
  if (asOf) asOf.textContent = `数据截至 ${data.as_of}`;
  dgRender();
  dgSyncCompareBtn();
}

/* ---------- 渲染 ---------- */

function dgRender() {
  const nodes = document.getElementById("dg-nodes");
  const edges = document.getElementById("dg-edges");
  const groups = document.getElementById("dg-groups");
  if (!nodes || !dg.layout) return;

  // 分组框画在最底层：它是背景分区，不是节点，也不参与连线。
  // 按层级从浅到深排序，深的画在上面，嵌套关系才看得出来。
  groups.innerHTML = dg.layout.nodes.filter(dgIsGroup)
    .map((g) => ({ g, d: dgDepth(g.id) }))
    .sort((a, b) => a.d - b.d)
    .map(({ g, d }) => {
      const c = DG_DEPTH[Math.min(d, DG_DEPTH.length - 1)];
      const trail = d ? dgTrail(g.id) : "";
      // 标签做成实心色块：分组框的底色很淡，纯文字压在上面读不出来，
      // 而层级本身也要一眼可辨——靠色块颜色，不靠字号差那 0.8px。
      const text = (trail ? `${trail} › ` : "") + g.label;
      const pw = text.length * (d ? 7.4 : 8.2) + 18;
      return `
    <g class="dg-node dg-group" data-node="${dgEsc(g.id)}" transform="translate(${g.x},${g.y})"
       style="cursor:${dg.edit ? "grab" : "default"}">
      <rect width="${g.w}" height="${g.h}" rx="${12 - d * 2}" fill="${c.fill}"
            fill-opacity="${c.fillOpacity}" stroke="${c.stroke}"
            stroke-width="${c.width}" stroke-dasharray="${c.dash}"/>
      <rect x="9" y="-9.5" width="${pw}" height="20" rx="10" fill="var(--surface)"/>
      <rect x="9" y="-9.5" width="${pw}" height="20" rx="10" fill="${c.stroke}" opacity="${c.pill}"/>
      <text x="${9 + pw / 2}" y="4.5" text-anchor="middle" font-size="${d ? 11 : 12}"
            font-weight="800" fill="${c.label}">${
        trail ? `<tspan opacity="0.62">${dgEsc(trail)} › </tspan>` : ""}${dgEsc(g.label)}</text>
      ${dgGroupAgg(g, c)}
      ${dg.edit ? dgHandles(g) : ""}
    </g>`;
    }).join("");

  const by = Object.fromEntries(dg.layout.nodes.map((n) => [n.id, n]));
  edges.innerHTML = (dg.layout.edges || []).map((e) => {
    const a = by[e.from], b = by[e.to];
    if (!a || !b) return "";
    // 分组之间的连线原来被直接跳过，所以「结构」只能靠节点两两连，
    // 二十来条线互相穿插，谁流向谁反而看不出来。现在分组也能连——
    // 物料流向本来就是环节之间的事，不是某个具体指标之间的事。
    const group = dgIsGroup(a) && dgIsGroup(b);
    // 锚点按相对位置选：同一列上下堆叠的两个框，用左右锚点会画出一条倒着绕回去的线。
    const stacked = a.x < b.x + b.w && b.x < a.x + a.w;
    let d;
    if (stacked) {
      // 上下堆叠的两个框之间只有十几像素，一小段竖线等于看不见。画成**肘形**：
      // 从父框左下角下来，拐进子框左边——树状图的通用画法，表达的是「包含」，
      // 而不是物料从上流到下。（这是画出来的图形，不是标签里的 └ 符号。）
      const x = a.x + 12;
      d = `M ${x} ${a.y + a.h} L ${x} ${b.y + b.h / 2} L ${b.x} ${b.y + b.h / 2}`;
    } else {
      const back = b.x + b.w < a.x;                 // 反向（右往左）时从左边出
      const x1 = back ? a.x : a.x + a.w, x2 = back ? b.x + b.w : b.x;
      const y1 = a.y + a.h / 2, y2 = b.y + b.h / 2, m = (x1 + x2) / 2;
      d = `M ${x1} ${y1} C ${m} ${y1}, ${m} ${y2}, ${x2} ${y2}`;
    }
    return `<path d="${d}"
      fill="none" stroke="var(--text-muted)" stroke-width="${group ? 3 : 1.6}"
      opacity="${group ? 0.9 : 0.5}" stroke-linecap="round"
      ${group ? "" : 'stroke-dasharray="5 4"'}
      marker-end="url(#dg-arrow${group ? "" : "-thin"})"/>`;
  }).join("");

  nodes.innerHTML = dg.layout.nodes.filter((n) => !dgIsGroup(n)).map((n) => dgNode(n)).join("");

  // 一张空图给的是一块白板，看不出该干什么。说清楚两条路：自己搭，或把大纲导进来。
  const blank = document.getElementById("dg-blank");
  if (blank) blank.classList.toggle("hidden", dg.layout.nodes.length > 0);

  dgApplyTransform();
}

const dgIsGroup = (n) => n.kind === "group";

function dgDepth(id, seen) {
  const n = dg.layout.nodes.find((x) => x.id === id);
  if (!n || !n.parent || (seen || new Set()).has(id)) return 0;
  const s = seen || new Set();
  s.add(id);
  return 1 + dgDepth(n.parent, s);
}

function dgTrail(id) {
  // 面包屑：「供给端 › 矿端」。层级靠文字说清楚，不指望所有人都懂 Markdown 的缩进符号。
  const trail = [];
  let cur = dg.layout.nodes.find((n) => n.id === id);
  const seen = new Set();
  while (cur && cur.parent && !seen.has(cur.parent)) {
    seen.add(cur.parent);
    cur = dg.layout.nodes.find((n) => n.id === cur.parent);
    if (cur) trail.unshift(cur.label);
  }
  return trail.join(" › ");
}

function dgToCanvas(svg, e) {
  const r = svg.getBoundingClientRect();
  return { x: (e.clientX - r.left - dg.pan.x) / dg.zoom,
           y: (e.clientY - r.top - dg.pan.y) / dg.zoom };
}

function dgNodeAt(pt, wantGroup) {
  // 按坐标找框，不靠 DOM 命中测试。
  //
  // 拖线时调了 setPointerCapture，于是 pointerup 的 e.target **永远是 SVG 本身**，
  // 不是光标底下那个框——`e.target.closest(".dg-node")` 恒为 null，连线因此必然
  // 报「松手时不在任何方框上」。这是 pointer capture 的固有行为，不是偶发。
  //
  // 顺带解决一件事：从分组往分组连时，松手位置往往落在目标分组里的某个节点上。
  // 按来源类型优先匹配同类，就不必要求使用者精确地松在分组的空白处。
  const hit = (n) => n.x <= pt.x && pt.x <= n.x + n.w && n.y <= pt.y && pt.y <= n.y + n.h;
  const all = dg.layout.nodes.filter(hit);
  const same = all.filter((n) => dgIsGroup(n) === !!wantGroup);
  const pool = same.length ? same : all;
  // 同类里取最深的那个：嵌套分组要命中最里层，节点画在分组之上
  return pool.sort((x, y) => dgDepth(y.id) - dgDepth(x.id))[0] || null;
}

function dgConnect(a, b) {
  if (!b) { showToast("连线取消：松手的位置不在任何方框上"); return; }
  if (a.id === b.id) { showToast("不能连到自己"); return; }
  if (dgIsGroup(a) !== dgIsGroup(b)) {
    // 分组连节点表达不了任何东西：分组已经"包含"了它的节点，再画一条箭头只会误导
    showToast("分组只能连分组，节点只能连节点 —— 包含关系用拖进框表达，不用连线");
    return;
  }
  dg.layout.edges = dg.layout.edges || [];
  const at = dg.layout.edges.findIndex((x) => x.from === a.id && x.to === b.id);
  const back = dg.layout.edges.findIndex((x) => x.from === b.id && x.to === a.id);
  dgPushUndo();
  if (at >= 0) {
    dg.layout.edges.splice(at, 1);
    showToast(`已删除连线「${a.label} → ${b.label}」`);
  } else {
    if (back >= 0) dg.layout.edges.splice(back, 1);   // 反向连一次即改向
    dg.layout.edges.push({ from: a.id, to: b.id });
    showToast(`已连「${a.label} → ${b.label}」${back >= 0 ? "（方向已反转）" : ""}`);
  }
  dg.dirty = true;
  dgSyncSave();
  dgRender();
}

function dgSubtree(id) {
  // 一个分组和它里面的全部内容（含子分组）。拖动、删除都该以这个为单位。
  const out = [];
  const walk = (pid) => {
    for (const n of dg.layout.nodes) {
      if (n.parent === pid && !out.includes(n)) { out.push(n); if (dgIsGroup(n)) walk(n.id); }
    }
  };
  const root = dg.layout.nodes.find((n) => n.id === id);
  if (root) { out.push(root); walk(id); }
  return out;
}

/* ---------- 撤销 ----------
 * 画布编辑没有撤销，一次误拖就得靠手动摆回去——而归属变化是看不见的，
 * 等发现时已经不知道该怎么还原了。整份布局做快照最简单也最可靠：
 * 这份 JSON 只有几十 KB，存 30 步完全不是负担。 */
const dgSnapshot = () => JSON.stringify({ nodes: dg.layout.nodes, edges: dg.layout.edges });

function dgSyncHistory() {
  const u = document.getElementById("dg-undo"), r = document.getElementById("dg-redo");
  if (u) { u.disabled = !(dg.undo || []).length; u.title = `后退一步 Ctrl+Z（${(dg.undo || []).length}）`; }
  if (r) { r.disabled = !(dg.redo || []).length; r.title = `前进一步 Ctrl+Shift+Z（${(dg.redo || []).length}）`; }
}

function dgPushUndo() {
  dg.undo = dg.undo || [];
  dg.undo.push(dgSnapshot());
  if (dg.undo.length > 40) dg.undo.shift();
  dg.redo = [];      // 新改动让"前进"失效，否则会接到一条已经不存在的历史上
  dgSyncHistory();
}

function dgStep(from, to, word) {
  if (!from.length) { showToast(`没有可${word}的操作`); return; }
  to.push(dgSnapshot());
  const s = JSON.parse(from.pop());
  dg.layout.nodes = s.nodes;
  dg.layout.edges = s.edges;
  dg.dirty = true;
  dgSyncSave();
  dgRender();
  dgLoad();
  dgSyncHistory();
  showToast(`已${word}（还可${word} ${from.length} 步）`);
}

function dgUndo() { dg.undo = dg.undo || []; dg.redo = dg.redo || []; dgStep(dg.undo, dg.redo, "后退"); }
function dgRedo() { dg.undo = dg.undo || []; dg.redo = dg.redo || []; dgStep(dg.redo, dg.undo, "前进"); }

function dgIsAncestor(maybeAncestor, id) {
  let cur = dg.layout.nodes.find((n) => n.id === id);
  const seen = new Set();
  while (cur && cur.parent && !seen.has(cur.parent)) {
    if (cur.parent === maybeAncestor) return true;
    seen.add(cur.parent);
    cur = dg.layout.nodes.find((n) => n.id === cur.parent);
  }
  return false;
}

function dgGroupAt(node) {
  // 拖动结束后按中心点落在哪个分组里决定归属；嵌套时取最深的那个。
  //
  // 必须排除自己的**后代**，不只是自己：把「供给端」拖进它自己的子分组「矿端」，
  // 归属就成了 供给端→矿端→供给端 的环，标签渲染成「供给端 › 矿端 › 供给端」，
  // 整个层级关系失去意义。
  const cx = node.x + node.w / 2, cy = node.y + node.h / 2;
  const hit = dg.layout.nodes.filter((g) =>
    dgIsGroup(g) && g.id !== node.id && !dgIsAncestor(node.id, g.id) &&
    g.x <= cx && cx <= g.x + g.w && g.y <= cy && cy <= g.y + g.h);
  if (!hit.length) return null;
  return hit.sort((a, b) => dgDepth(b.id) - dgDepth(a.id))[0].id;
}

function dgHealCycles() {
  // 已经存成环的布局要能自己修好——否则打开就是一张层级全乱的图，还不知道为什么。
  const by = Object.fromEntries(dg.layout.nodes.map((n) => [n.id, n]));
  const broken = [];
  for (const n of dg.layout.nodes) {
    const seen = new Set([n.id]);
    let cur = n;
    while (cur && cur.parent) {
      if (seen.has(cur.parent)) { broken.push(n); break; }
      seen.add(cur.parent);
      cur = by[cur.parent];
    }
  }
  for (const n of broken) n.parent = null;
  if (broken.length) {
    dg.dirty = true;
    showToast(`检测到 ${broken.length} 处分组归属成环（父框被拖进了自己的子框），已解除归属，请重新拖入并保存`);
  }
  return broken.length;
}

/* 缩放：拖边框本身，不画把手。
 *
 * 八个小方点把每个框都点缀得像个选中态控件，二十多个框一起显示就是一地的点。
 * 改成沿边框铺一圈**透明命中区**——看不见，但鼠标移上去光标会变成缩放箭头，
 * 这是绘图工具的通用做法，不用画出来也不用教。
 *
 * 命中区要盖住边框内外各几像素，否则得像素级对准才拖得到。 */
const GRIP = 7;   // 命中区厚度（画布坐标）
const DG_GRIPS = [
  ["nw", "nwse-resize"], ["n", "ns-resize"], ["ne", "nesw-resize"],
  ["w", "ew-resize"], ["e", "ew-resize"],
  ["sw", "nesw-resize"], ["s", "ns-resize"], ["se", "nwse-resize"],
];

function dgHandles(n) {
  const G = GRIP, C = GRIP * 2;
  const box = (dir, x, y, w, h, cursor) =>
    `<rect class="dg-resize" data-resize="${dgEsc(n.id)}" data-dir="${dir}"
      x="${x}" y="${y}" width="${Math.max(1, w)}" height="${Math.max(1, h)}"
      fill="transparent" style="cursor:${cursor}"/>`;
  const geo = {
    n:  [C - G, -G, n.w - C * 2 + G * 2, G * 2],
    s:  [C - G, n.h - G, n.w - C * 2 + G * 2, G * 2],
    w:  [-G, C - G, G * 2, n.h - C * 2 + G * 2],
    e:  [n.w - G, C - G, G * 2, n.h - C * 2 + G * 2],
    nw: [-G, -G, C, C],
    ne: [n.w - G, -G, C, C],
    sw: [-G, n.h - G, C, C],
    se: [n.w - G, n.h - G, C, C],
  };
  // 角在后面画，压住边——角上同时属于两条边时应当走对角缩放
  return DG_GRIPS.sort((a, b) => a[0].length - b[0].length)
    .map(([dir, cursor]) => box(dir, ...geo[dir], cursor)).join("");
}

function dgGroupAgg(g, c) {
  // 分组自己的合计值：右上角一块，和标题分开——它是这个框的数，不是框的名字
  const v = dg.values[g.id];
  if (!v || !v.agg) return "";
  const blocked = v.state === "blocked" || v.state === "no_data" || v.state === "unbound";
  const label = v.agg === "share" ? "占比" : "合计";
  const body = blocked
    ? `<tspan fill="var(--amber)">${dgEsc(label)}不出数</tspan>`
    : `<tspan font-weight="800">${dgEsc(dgNum(v.value, v.unit))}</tspan>`;
  const w = (blocked ? 60 : String(dgNum(v.value, v.unit)).length * 7 + 34);
  return `
    <g transform="translate(${g.w - 9 - w},-9.5)" style="cursor:pointer">
      <rect width="${w}" height="20" rx="10" fill="var(--surface)"/>
      <rect width="${w}" height="20" rx="10" fill="${c.stroke}" opacity="${c.pill}"/>
      <text x="${w / 2}" y="14" text-anchor="middle" font-size="10.5" fill="${c.label}">
        <tspan opacity="0.7">${dgEsc(label)} </tspan>${body}</text>
      <title>${dgEsc(v.note || "")}</title>
    </g>`;
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
  const proxy = v.proxy;
  const roles = v.roles || [];
  const dim = dg.judgmentView && !roles.length;
  const hasValue = v.state === "ok" || v.state === "stale";
  // 左侧色条只表示涨跌方向；边框仍然表示数据状态，两者不能混为一谈
  const dirColor = hasValue ? dgDir(v.delta != null ? v.delta : v.mom).color : null;

  // 一个节点只绑一个指标，方框要说的就一件事：这个数现在多少、在往哪走、什么时候的。
  // 同比、静态系数这些挪进详情抽屉——挤在方框里会把框撑大，二十几个框摆不下一屏。
  let main = "";
  if (hasValue) {
    const d = dgDir(v.delta != null ? v.delta : v.mom);
    const parts = [];
    if (v.delta != null) parts.push(dgNum(v.delta, "").replace(/^-/, "−"));
    if (dgPct(v.mom) != null) parts.push(dgPct(v.mom));
    main = `
      <div class="text-[13px] font-bold tabular leading-none mt-[3px] truncate">${dgEsc(dgNum(v.value, v.unit))}</div>
      ${parts.length ? `<div class="${d.cls} text-[9.5px] font-semibold tabular leading-none mt-[3px] truncate">
        ${d.mark} ${parts.join(" ")}</div>` : ""}
      <div class="text-[8.5px] text-[var(--text-muted)] tabular leading-none mt-[3px] truncate">
        ${dgEsc(v.frequency || "")}·${dgEsc((v.as_of || "").slice(2))}</div>`;
  } else if (v.state === "blocked" || v.state === "missing_input") {
    // 两者都留空，但要说清楚是"补数据就能算"还是"口径对不上，补也没用"
    main = `<div class="text-[10px] font-semibold mt-1">${
      v.state === "blocked" ? "阻断不出数" : "缺少输入"}</div>
      <div class="text-[9px] mt-0.5 leading-snug">${dgEsc((v.note || "").slice(0, 24))}</div>`;
  } else if (v.state === "no_data" || v.state === "retired") {
    main = `<div class="text-[10px] font-semibold mt-1">${
      v.state === "retired" ? "指标已停用" : "尚无数据"}</div>`;
  }

  // 静态系数只在没有数值时占位——否则那个框就真空了（纯调研节点本来就靠它说话）。
  // 有数值时缩成右下角一个角标：多一行文字就把 76px 的框撑破，底部会被裁掉。
  const statics = (n.statics || []);
  const staticRows = hasValue ? "" : statics.slice(0, 2).map((s) =>
    `<div class="text-[9px] text-[var(--text-muted)] leading-tight truncate">［静］${
      dgEsc(s.label)} ${dgEsc(s.value)}</div>`).join("");
  const staticMark = (hasValue && statics.length)
    ? `<text x="${n.w - 6}" y="${n.h - 5}" text-anchor="end" font-size="8"
             fill="var(--text-muted)" opacity="0.75">静${statics.length}<title>${
        dgEsc(statics.map((s) => `${s.label} ${s.value}`).join("　"))}</title></text>` : "";

  // 角色只在判断视角下展开成文字；平时缩成一个点，不跟标题抢那一行
  const roleMark = roles.length
    ? (dg.judgmentView
        ? `<g transform="translate(${n.w - 6},5)">
             <rect x="${-roles[0].length * 9 - 8}" y="0" width="${roles[0].length * 9 + 8}"
                   height="13" rx="6.5" fill="var(--primary)" opacity="0.16"/>
             <text x="-4" y="9.8" text-anchor="end" font-size="8.5" font-weight="700"
                   fill="var(--primary)">${dgEsc(roles[0])}</text></g>`
        : `<circle cx="${n.w - 8}" cy="8" r="3" fill="var(--primary)" opacity="0.7"><title>${
             dgEsc(roles.join("·"))}</title></circle>`)
    : "";

  return `
  <g class="dg-node" data-node="${dgEsc(n.id)}" transform="translate(${n.x},${n.y})"
     opacity="${dim ? 0.22 : 1}" style="cursor:${dg.edit ? "grab" : "pointer"}">
    <rect width="${n.w}" height="${n.h}" rx="6" fill="${st.fill}" stroke="${st.stroke}"
          stroke-width="${proxy ? 1.4 : 1.6}" ${st.dash ? `stroke-dasharray="${st.dash}"` : ""}/>
    ${proxy ? `<rect width="${n.w}" height="${n.h}" rx="6" fill="url(#dg-hatch)" opacity="0.5"/>` : ""}
    ${dirColor ? `<path d="M 0 6 A 6 6 0 0 1 6 0 L 4 0 L 4 ${n.h} L 6 ${n.h} A 6 6 0 0 1 0 ${n.h - 6} Z"
        fill="${dirColor}" opacity="0.9"/>` : ""}
    ${roleMark}
    ${staticMark}
    ${dg.edit ? dgHandles(n) : ""}
    <foreignObject x="${dirColor ? 11 : 8}" y="6" width="${n.w - (dirColor ? 20 : 16)}" height="${n.h - 10}">
      <div xmlns="http://www.w3.org/1999/xhtml" class="${st.tone}" style="font-family:inherit">
        <div class="text-[10px] font-bold" style="line-height:1.15;${
          roles.length && !dg.judgmentView ? "padding-right:9px;" : ""
          }display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${
          dgEsc(n.label)}${proxy ? " ◍" : ""}</div>
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
  // 空布局要挡住：Math.max(...[]) 是 -Infinity，算出来的 zoom 是 NaN，
  // 整张画布的 transform 直接失效——新建一张空图就会撞上。
  if (!dg.layout.nodes.length) {
    dg.zoom = 1;
    dg.pan = { x: 0, y: 0 };
    dgApplyTransform();
    return;
  }
  const xs = dg.layout.nodes.map((n) => n.x), ys = dg.layout.nodes.map((n) => n.y);
  const w = Math.max(...dg.layout.nodes.map((n) => n.x + n.w)) - Math.min(...xs);
  const h = Math.max(...dg.layout.nodes.map((n) => n.y + n.h)) - Math.min(...ys);
  const pad = 40;
  dg.zoom = Math.min((wrap.clientWidth - pad) / Math.max(w, 1),
                     (wrap.clientHeight - pad) / Math.max(h, 1), 1.4);
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
      drag = { node, resize: true, dir: handle.dataset.dir || "se",
               x0: e.clientX, y0: e.clientY,
               w0: node.w, h0: node.h, nx: node.x, ny: node.y };
      dgPushUndo();
      svg.setPointerCapture(e.pointerId);
    } else if (dg.edit && g && (e.shiftKey || dg.linking)) {
      // 手动连线：按住 Shift 从一个框拖到另一个框。连线原来只能靠 Markdown 缩进
      // 自动生成，分组之间的主链路在画布上根本画不出来。
      const node = dg.layout.nodes.find((n) => n.id === g.dataset.node);
      drag = { link: node, x0: e.clientX, y0: e.clientY };
      svg.setPointerCapture(e.pointerId);
    } else if (dg.edit && g) {
      const node = dg.layout.nodes.find((n) => n.id === g.dataset.node);
      // 拖分组要把里面的东西一起带走。原来只挪框本身，框滑开、成员留在原地，
      // 几何归属立刻全乱——这就是「手拖动极易改变分组关系」的主要来源。
      const family = dgIsGroup(node) ? dgSubtree(node.id) : [node];
      drag = { node, family: family.map((n) => ({ n, x: n.x, y: n.y })),
               x0: e.clientX, y0: e.clientY, moved: false };
      dgPushUndo();
      svg.setPointerCapture(e.pointerId);
    } else if (!dg.edit && g) {
      // 看图模式：记下点的是哪个节点，但仍然允许拖动画布平移
      const node = dg.layout.nodes.find((n) => n.id === g.dataset.node);
      drag = { node, pan: true, x0: e.clientX, y0: e.clientY, px: dg.pan.x, py: dg.pan.y };
      svg.setPointerCapture(e.pointerId);
    } else {
      drag = { pan: true, x0: e.clientX, y0: e.clientY, px: dg.pan.x, py: dg.pan.y };
      svg.setPointerCapture(e.pointerId);
    }
  });

  svg.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x0, dy = e.clientY - drag.y0;
    if (drag.link) {
      const a = drag.link;
      const pt = dgToCanvas(svg, e);
      let rope = document.getElementById("dg-rope");
      if (!rope) {
        rope = document.createElementNS("http://www.w3.org/2000/svg", "path");
        rope.id = "dg-rope";
        rope.setAttribute("fill", "none");
        rope.setAttribute("stroke", "var(--primary)");
        rope.setAttribute("stroke-width", "2");
        rope.setAttribute("stroke-dasharray", "5 4");
        document.getElementById("dg-edges").appendChild(rope);
      }
      rope.setAttribute("d",
        `M ${a.x + a.w / 2} ${a.y + a.h / 2} L ${pt.x} ${pt.y}`);
      // 高亮此刻会连到谁。看不见候选目标，就只能靠试。
      const cand = dgNodeAt(pt, dgIsGroup(a));
      document.querySelectorAll(".dg-link-target").forEach((el) =>
        el.classList.remove("dg-link-target"));
      if (cand && cand.id !== a.id) {
        document.querySelector(`[data-node="${cand.id}"] > rect`)
          ?.classList.add("dg-link-target");
      }
      rope.setAttribute("stroke", cand && cand.id !== a.id
        ? "var(--primary)" : "var(--text-muted)");
      return;
    }
    if (drag.pan) {
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) drag.moved = true;
      dg.pan = { x: drag.px + dx, y: drag.py + dy };
      dgApplyTransform();
      return;
    }
    if (drag.resize) {
      // 往左/往上拖要同时改位置和尺寸——只改尺寸的话框会朝反方向长出去。
      // 下限保证框里还装得下标签与一行数值，不至于被拖成一条缝。
      const MINW = 100, MINH = 52;
      const gx = Math.round(dx / dg.zoom / 10) * 10, gy = Math.round(dy / dg.zoom / 10) * 10;
      const d = drag.dir;
      let { nx: x, ny: y, w0: w, h0: h } = drag;
      if (d.includes("e")) w = Math.max(MINW, drag.w0 + gx);
      if (d.includes("s")) h = Math.max(MINH, drag.h0 + gy);
      if (d.includes("w")) { w = Math.max(MINW, drag.w0 - gx); x = drag.nx + (drag.w0 - w); }
      if (d.includes("n")) { h = Math.max(MINH, drag.h0 - gy); y = drag.ny + (drag.h0 - h); }
      Object.assign(drag.node, { x, y, w, h });
      dg.dirty = true;
      dgRender();
      return;
    }
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) drag.moved = true;
    const ox = Math.round(dx / dg.zoom / 10) * 10;   // 对齐到 10px 网格
    const oy = Math.round(dy / dg.zoom / 10) * 10;
    for (const m of drag.family) { m.n.x = m.x + ox; m.n.y = m.y + oy; }
    dg.dirty = true;
    dgRender();
  });

  svg.addEventListener("pointerup", (e) => {
    const wasDrag = drag;
    drag = null;
    svg.releasePointerCapture?.(e.pointerId);
    document.getElementById("dg-rope")?.remove();
    if (wasDrag && wasDrag.link) {
      document.querySelectorAll(".dg-link-target").forEach((el) =>
        el.classList.remove("dg-link-target"));
      dgConnect(wasDrag.link,
                dgNodeAt(dgToCanvas(svg, e), dgIsGroup(wasDrag.link)));
      return;
    }
    if (dg.edit && wasDrag && wasDrag.node && !wasDrag.resize && wasDrag.moved) {
      // 拖到哪个分组里就归哪个组。归属是显式字段，不靠画完之后再猜几何包含。
      const before = wasDrag.node.parent || null;
      const now = dgGroupAt(wasDrag.node);
      if (before !== now) {
        wasDrag.node.parent = now;
        // 停在手松开的地方多半和别的框叠着，顺手排整齐；旧分组也要重排，把空位收掉
        dgAutoArrange(now, before);
        dgRender();
        // 归属变化是看不见的结构改动，必须说出来，并且当场给一条退路
        showToast((now
          ? `「${wasDrag.node.label}」已归入「${dg.layout.nodes.find((n) => n.id === now).label}」并排好位置`
          : `「${wasDrag.node.label}」已移出分组`) + " —— 不对就按 Ctrl+Z 撤销");
      }
    }
    if (wasDrag && wasDrag.node && !wasDrag.resize && !wasDrag.moved) {
      // 编辑模式点击是"换绑"，看图模式点击是"看清楚这个数凭什么可信"。
      // 分组框在看图模式下原来点了毫无反应——但它其实是个实体：有成员、有合计、
      // 有自己的属性，该有自己的抽屉。
      if (dg.edit) dgOpenPool(wasDrag.node.id);
      else if (dgIsGroup(wasDrag.node)) dgOpenGroup(wasDrag.node.id);
      else dgOpenDetail(wasDrag.node.id);
    }
    dgSyncSave();
  });

  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    const k = e.key.toLowerCase();
    if (k !== "z" && k !== "y") return;
    if (/^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName || "")) return;
    e.preventDefault();
    if (k === "y" || e.shiftKey) dgRedo();
    else dgUndo();
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
  const all = dg.pool
    .filter((f) => f.kind !== "meta")
    .filter((f) => !q || f.label.toLowerCase().includes(q) || f.field.toLowerCase().includes(q));

  // 近 400 条拍平成一个长列表，即便能搜也看不出全貌：不知道有哪些类别、
  // 每类有多少、自己关心的那类在哪。按类别折叠——默认全收起，只看见目录。
  const cats = new Map();
  for (const f of all) {
    if (!cats.has(f.group)) cats.set(f.group, []);
    cats.get(f.group).push(f);
  }
  document.getElementById("dg-pool-count").textContent =
    `共 ${all.length} 条 · ${cats.size} 个类别${q ? "（已按搜索过滤）" : ""}`;

  if (!all.length) {
    document.getElementById("dg-pool").innerHTML =
      '<div class="py-6 text-center text-[var(--text-muted)]">没有匹配的指标</div>';
    return;
  }

  dg.openCats = dg.openCats || new Set();
  // 搜索时全部展开（此时结果本来就少）；已绑的那一类也自动展开，否则看不到绑的是什么
  const boundCat = bound ? (all.find((f) => f.field === bound) || {}).group : null;
  const LIMIT = 60;   // 单个类别内的上限，避免一类几百条撑爆

  const item = (f) => `
    <button type="button" onclick="dgBind('${dgEsc(f.field)}','${dgEsc(f.kind)}')"
      class="w-full text-left pl-4 pr-2 py-1.5 rounded hover:bg-[var(--surface-soft)] ${
        f.field === bound ? "bg-[var(--primary-soft)]" : ""}">
      <div class="font-medium truncate">${f.field === bound ? "✓ " : ""}${dgEsc(f.label)}${
        f.unit ? `（${dgEsc(f.unit)}）` : ""}</div>
      <div class="text-[10px] text-[var(--text-muted)] truncate">${dgEsc(f.field)}${
        usedBy[f.field] ? ` · <span class="text-[var(--primary)]">已绑于「${dgEsc(usedBy[f.field])}」</span>` : ""}</div>
    </button>`;

  document.getElementById("dg-pool").innerHTML = [...cats.entries()].map(([name, fields]) => {
    const open = !!q || dg.openCats.has(name) || name === boundCat;
    const used = fields.filter((f) => usedBy[f.field] || f.field === bound).length;
    const shown = open ? fields.slice(0, LIMIT) : [];
    return `
    <div class="border-b border-[var(--line)] last:border-0">
      <button type="button" onclick="dgToggleCat('${dgEsc(name)}')"
        class="w-full flex items-center gap-1.5 px-1 py-2 text-left hover:bg-[var(--surface-soft)]">
        <span class="text-[var(--text-muted)] w-3">${open ? "▾" : "▸"}</span>
        <span class="font-semibold flex-1 truncate">${dgEsc(name)}</span>
        ${used ? `<span class="text-[10px] text-[var(--primary)]">已绑 ${used}</span>` : ""}
        <span class="text-[10px] text-[var(--text-muted)] tabular">${fields.length}</span>
      </button>
      ${shown.map(item).join("")}
      ${open && fields.length > LIMIT
        ? `<div class="pl-4 py-1.5 text-[10px] text-[var(--text-muted)]">
             这一类还有 ${fields.length - LIMIT} 条未显示 —— 用上面的搜索缩小范围</div>` : ""}
    </div>`;
  }).join("");
}

function dgToggleCat(name) {
  dg.openCats = dg.openCats || new Set();
  if (dg.openCats.has(name)) dg.openCats.delete(name);
  else dg.openCats.add(name);
  dgRenderPool();
}

function dgBind(field, kind) {
  dgPushUndo();
  const node = dg.layout.nodes.find((n) => n.id === dg.active);
  if (!node) return;
  // 一个节点只绑一个指标，那么默认就该叫指标的名字——不然新建的框全叫"新节点"，
  // 得逐个改名才看得出是什么。只在用户没起过名时替换，手工改过的名字不动。
  const prev = (node.binding || {}).series_id || (node.binding || {}).formula_id;
  const prevName = prev ? (dg.pool.find((f) => f.field === prev) || {}).label : null;
  if (!node.label || node.label === "新节点" || node.label === prev || node.label === prevName) {
    const picked = dg.pool.find((f) => f.field === field);
    if (picked && picked.label) node.label = picked.label;
  }
  node.binding = kind === "derived"
    ? { kind: "derived", formula_id: field }
    : { kind: "series", series_id: field, proxy: document.getElementById("dg-proxy").checked };
  dg.dirty = true;
  dgSyncSave();
  dgLoad();
  toggleDrawer("dg-drawer", false);
}

function dgUnbind() {
  dgPushUndo();
  const node = dg.layout.nodes.find((n) => n.id === dg.active);
  if (!node) return;
  node.binding = { kind: "none" };
  dg.dirty = true;
  dgSyncSave();
  dgLoad();
  toggleDrawer("dg-drawer", false);
}

function dgMarkProxy() {
  const node = dg.layout.nodes.find((n) => n.id === dg.active);
  if (!node || !node.binding || node.binding.kind !== "series") return;
  node.binding.proxy = document.getElementById("dg-proxy").checked;
  dg.dirty = true;
  dgSyncSave();
  dgLoad();
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
  if (templateId === "__new") { dgNewTemplate(); return; }
  if (dg.dirty && !confirm("当前布局有未保存的改动，切换会丢失。继续？")) {
    const sel = document.getElementById("dg-template");
    if (sel) sel.value = dg.layout.__id;
    return;
  }
  window.location.href = `/sn/diagram?template=${templateId}`;
}

async function dgNewTemplate() {
  // 之前只能从已有布局里挑，或者走 Markdown 导入——想从一张白纸开始是做不到的。
  const sel = document.getElementById("dg-template");
  const name = (prompt("新布局叫什么？", "未命名结构图") || "").trim();
  if (!name) { if (sel) sel.value = dg.layout.__id; return; }
  const res = await fetch("/api/sn/diagram/templates", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, layout: { nodes: [], edges: [] } }),
  });
  const data = await res.json();
  if (!res.ok) {
    showToast(data.detail || "新建失败");
    if (sel) sel.value = dg.layout.__id;
    return;
  }
  window.location.href = `/sn/diagram?template=${data.saved.id}&edit=1`;
}

function dgDeleteTemplate() {
  const name = dg.layout.__name;
  if (!confirm(`确认删除布局「${name}」吗？此操作不可撤销。`)) return;
  fetch(`/api/sn/diagram/templates/${dg.layout.__id}`, { method: "DELETE" })
    .then((r) => r.json().then((d) => ({ ok: r.ok, d })))
    .then(({ ok, d }) => {
      if (!ok) { showToast(d.detail || "删除失败"); return; }
      window.location.href = "/sn/diagram";
    });
}

/* ---------- 启动 ---------- */

function dgInit() {
  const tpl = window.DG_TEMPLATE;
  if (!tpl) return;
  dg.layout = { ...tpl.layout, __id: tpl.id, __name: tpl.name };
  dg.layout.nodes = dg.layout.nodes || [];
  dg.layout.edges = dg.layout.edges || [];
  dgHealCycles();
  dgBindCanvas();
  dgRender();
  dgFit();
  dgLoad();
  dgSyncCompareBtn();
  // 新建的空图直接进编辑模式——否则落地看到一块白板，工具栏还藏着
  if (new URLSearchParams(location.search).get("edit") === "1" || !dg.layout.nodes.length) {
    if (!dg.edit) dgToggleEdit();
  }
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
  dgPushUndo();
  // 新节点落在当前视口左上角附近，而不是画布原点——否则在远处看不见
  const x = Math.round((-dg.pan.x / dg.zoom + 40) / 10) * 10;
  const y = Math.round((-dg.pan.y / dg.zoom + 40) / 10) * 10;
  const node = kind === "group"
    ? { id: dgNextId("g"), kind: "group", label: "新分组", x, y, w: 420, h: 260, binding: { kind: "none" }, statics: [] }
    : { id: dgNextId("n"), label: "新节点", x, y, w: 140, h: 66, binding: { kind: "none" }, statics: [] };
  dg.layout.nodes.push(node);
  // 新节点落在视口角上，多半正压在别的框上。落点在某个分组内就归进去并排好队，
  // 免得每加一个都要手动挪开。
  const into = dgGroupAt(node);
  if (into) { node.parent = into; dgAutoArrange(into); }
  dg.dirty = true;
  dgSyncSave();
  dgRender();
  if (kind !== "group") dgOpenPool(node.id);
}

function dgDeleteActive() {
  dgPushUndo();
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
  dgPushUndo();
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

async function dgImportMarkdown(into) {
  const msg = document.getElementById("dg-md-msg");
  if (into && !confirm(
      `确认用这份 Markdown 改写「${dg.layout.__name}」吗？\n\n` +
      "结构（分组、层级、绑定、静态标注）按文本重建，但**方框的位置和大小会重新排布**——" +
      "手工摆过的版面会丢。不确定就先用「导入为新布局」。")) return;
  msg.textContent = "处理中…";
  const res = await fetch("/api/sn/diagram/markdown", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ markdown: document.getElementById("dg-md-text").value,
                           name: into ? "" : document.getElementById("dg-md-name").value,
                           into: into ? dg.layout.__id : null }),
  });
  const data = await res.json();
  if (!res.ok) { msg.textContent = data.detail || "失败"; return; }
  // 导入只给框和标签，绑定要靠「建议参考」逐个补——把这件事说清楚，别让人以为导完就完了
  msg.textContent = data.replaced
    ? `已改写「${data.saved.name}」，其中 ${data.unbound} 个节点尚未绑定指标，正在刷新…`
    : `已建「${data.saved.name}」，其中 ${data.unbound} 个节点尚未绑定指标，正在跳转…`;
  dg.dirty = false;   // 服务端已是最新，别再弹"未保存"
  setTimeout(() => { window.location.href = `/sn/diagram?template=${data.saved.id}`; }, 900);
}

function dgCopyMarkdown() {
  navigator.clipboard.writeText(document.getElementById("dg-md-text").value).then(
    () => showToast("已复制 Markdown"), () => showToast("复制失败，请手动选择"));
}

/* ---------- 分组整理 ---------- */

function dgTidyGroups() {
  dgPushUndo();
  // 整张图重排：每个顶层分组递归排好内容，再收拢边框。
  //
  // 单个分组的重排是自动的（加节点、拖进分组时就地排好），但**整张图的重排只在点
  // 这个按钮时发生**——自动重排整张图会让人失去对布局的掌控：刚摆好的位置被系统
  // 改掉，比对不齐更难受。
  const ids = new Set(dg.layout.nodes.filter(dgIsGroup).map((n) => n.id));
  const roots = dg.layout.nodes.filter((n) => dgIsGroup(n) && !ids.has(n.parent));
  let arranged = 0;
  for (const r of roots) arranged += dgArrange(r.id);
  if (arranged) { dg.dirty = true; dgSyncSave(); dgRender(); }
  showToast(arranged ? `已排布 ${arranged} 个元素` : "没有需要排布的分组");
}

/* ---------- 节点详情（看图模式点击） ---------- */

function dgFmt(v) {
  const a = Math.abs(v);
  return v.toLocaleString("zh-CN", { maximumFractionDigits: a >= 1000 ? 0 : a >= 10 ? 1 : 2 });
}

function dgAxisDate(ticks) {
  // 横轴标签的粒度按**实际要标出来的那几个刻度**挑，取第一个不重复的格式。
  //
  // 按跨度拍粒度会两头不讨好：一律 MM-DD，五年月度序列标成「08-31 08-31 09-30 08-31」，
  // 年份——那张图上唯一有信息量的部分——反而被切掉；一律标年，两年半的周度序列又标成
  // 「2024 2024 2025 2025 2026」。重复的刻度等于没有刻度。
  const forms = [(s) => s.slice(0, 4), (s) => s.slice(2, 7), (s) => s.slice(2)];
  for (const f of forms) {
    const labels = ticks.map(f);
    if (new Set(labels).size === labels.length) return f;
  }
  return forms[forms.length - 1];
}

function dgChart(points, unit, w = 420, h = 180) {
  // 带坐标轴的走势图。只画一条序列——单位与量级不同的序列叠在一张图上会互相淹没，
  // 而双轴是明令禁用的（01 §4.6）。要比趋势走「对比」视图的归一化。
  if (!points || points.length < 2) return "";
  const PAD = { l: 58, r: 12, t: 12, b: 26 };
  const iw = w - PAD.l - PAD.r, ih = h - PAD.t - PAD.b;
  const vals = points.map((p) => p.value);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08;
  lo -= pad; hi += pad;
  const X = (i) => PAD.l + (i / (points.length - 1)) * iw;
  const Y = (v) => PAD.t + ih - ((v - lo) / (hi - lo)) * ih;
  const d = points.map((p, i) => `${i ? "L" : "M"} ${X(i).toFixed(1)} ${Y(p.value).toFixed(1)}`).join(" ");
  const rising = vals[vals.length - 1] >= vals[0];
  const color = rising ? "var(--up)" : "var(--down)";

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => lo + (hi - lo) * f);
  const grid = ticks.map((v) => `
    <line x1="${PAD.l}" y1="${Y(v).toFixed(1)}" x2="${w - PAD.r}" y2="${Y(v).toFixed(1)}"
          stroke="var(--line)" stroke-width="0.8" opacity="0.7"/>
    <text x="${PAD.l - 6}" y="${(Y(v) + 3).toFixed(1)}" text-anchor="end" font-size="9"
          fill="var(--text-muted)" class="tabular">${dgEsc(dgFmt(v))}</text>`).join("");

  const every = Math.max(1, Math.ceil(points.length / 5));
  const at = points.map((p, i) => i).filter((i) => i % every === 0 || i === points.length - 1);
  const fmtX = dgAxisDate(at.map((i) => points[i].as_of));
  const xlab = at.map((i) => `<text x="${X(i).toFixed(1)}" y="${h - 8}" text-anchor="middle"
      font-size="9" fill="var(--text-muted)" class="tabular">${dgEsc(fmtX(points[i].as_of))}</text>`).join("");

  // 每个数据点都可悬停读数——图上看出"在动"，鼠标指上去看清"动到多少"。
  // 十字线把光标位置投到两条坐标轴上：光有一个气泡，仍然要自己目测这个点对应哪一天、
  // 落在纵轴什么高度。
  const dots = points.map((p, i) => `
    <circle cx="${X(i).toFixed(1)}" cy="${Y(p.value).toFixed(1)}" r="9" fill="transparent"
            class="dg-dot" data-x="${X(i).toFixed(1)}" data-y="${Y(p.value).toFixed(1)}"
            data-date="${dgEsc(p.as_of.slice(2))}" data-val="${dgEsc(dgFmt(p.value))}${dgEsc(unit || "")}"
            data-label="${dgEsc(p.as_of)}　${dgEsc(dgFmt(p.value))}${dgEsc(unit || "")}"/>`).join("");
  const last = points[points.length - 1];

  return `<svg viewBox="0 0 ${w} ${h}" class="w-full" style="height:${h}px" id="dg-chart-svg"
               data-w="${w}" data-h="${h}" data-l="${PAD.l}" data-t="${PAD.t}"
               data-b="${(PAD.t + ih).toFixed(1)}" data-r="${w - PAD.r}">
    ${grid}${xlab}
    <line x1="${PAD.l}" y1="${PAD.t}" x2="${PAD.l}" y2="${PAD.t + ih}" stroke="var(--line-strong)" stroke-width="1"/>
    <line x1="${PAD.l}" y1="${PAD.t + ih}" x2="${w - PAD.r}" y2="${PAD.t + ih}" stroke="var(--line-strong)" stroke-width="1"/>
    <path d="${d} L ${X(points.length - 1).toFixed(1)} ${PAD.t + ih} L ${PAD.l} ${PAD.t + ih} Z"
          fill="${color}" opacity="0.08"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.8"/>
    <circle cx="${X(points.length - 1).toFixed(1)}" cy="${Y(last.value).toFixed(1)}" r="3" fill="${color}"/>
    <g id="dg-chart-cross" style="display:none" pointer-events="none">
      <line class="dg-cx" stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="3 3" opacity="0.8"/>
      <line class="dg-cy" stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="3 3" opacity="0.8"/>
      <circle class="dg-cdot" r="4" fill="var(--surface)" stroke="${color}" stroke-width="2"/>
      <g class="dg-cax"><rect rx="2" fill="var(--text)"/>
        <text font-size="9" fill="var(--surface)" class="tabular" text-anchor="middle"></text></g>
      <g class="dg-cay"><rect rx="2" fill="var(--text)"/>
        <text font-size="9" fill="var(--surface)" class="tabular" text-anchor="end"></text></g>
    </g>
    ${dots}
    <g id="dg-chart-tip" style="display:none" pointer-events="none">
      <rect rx="3" fill="var(--text)" opacity="0.92"/>
      <text font-size="10" fill="var(--surface)" class="tabular"></text>
    </g>
  </svg>`;
}

function dgBindChartHover() {
  const svg = document.getElementById("dg-chart-svg");
  if (!svg) return;
  const W = +svg.dataset.w, L = +svg.dataset.l, T = +svg.dataset.t;
  const B = +svg.dataset.b, R = +svg.dataset.r;
  const tip = svg.querySelector("#dg-chart-tip");
  const box = tip.querySelector("rect"), label = tip.querySelector("text");
  const cross = svg.querySelector("#dg-chart-cross");
  const cx = cross.querySelector(".dg-cx"), cy = cross.querySelector(".dg-cy");
  const cdot = cross.querySelector(".dg-cdot");
  const ax = cross.querySelector(".dg-cax"), ay = cross.querySelector(".dg-cay");

  const stamp = (g, text, x, y, anchor) => {
    const t = g.querySelector("text"), r = g.querySelector("rect");
    t.textContent = text;
    const w = text.length * 5.6 + 8;
    r.setAttribute("x", anchor === "end" ? x - w : x - w / 2);
    r.setAttribute("y", y - 8); r.setAttribute("width", w); r.setAttribute("height", 13);
    t.setAttribute("x", anchor === "end" ? x - 4 : x); t.setAttribute("y", y + 2);
    t.setAttribute("text-anchor", anchor);
  };

  svg.querySelectorAll(".dg-dot").forEach((dot) => {
    dot.addEventListener("mouseenter", () => {
      const x = +dot.dataset.x, y = +dot.dataset.y;
      cx.setAttribute("x1", x); cx.setAttribute("x2", x);
      cx.setAttribute("y1", T); cx.setAttribute("y2", B);
      cy.setAttribute("y1", y); cy.setAttribute("y2", y);
      cy.setAttribute("x1", L); cy.setAttribute("x2", R);
      cdot.setAttribute("cx", x); cdot.setAttribute("cy", y);
      stamp(ax, dot.dataset.date, x, B + 14, "middle");   // 投到横轴（YY-MM-DD，年份不能丢）
      stamp(ay, dot.dataset.val, L - 4, y, "end");                  // 投到纵轴
      cross.style.display = "";

      label.textContent = dot.dataset.label;
      const w = dot.dataset.label.length * 6.4 + 12;
      const left = Math.max(2, Math.min(x - w / 2, W - w - 2));
      box.setAttribute("x", left); box.setAttribute("y", y - 26);
      box.setAttribute("width", w); box.setAttribute("height", 17);
      label.setAttribute("x", left + 6); label.setAttribute("y", y - 14);
      tip.style.display = "";
    });
    dot.addEventListener("mouseleave", () => {
      tip.style.display = "none";
      cross.style.display = "none";
    });
  });
}

async function dgOpenDetail(nodeId) {
  const node = dg.layout.nodes.find((n) => n.id === nodeId);
  const v = dg.values[nodeId] || {};
  const box = document.getElementById("dg-detail-body");
  document.getElementById("dg-detail-title").textContent = node.label;
  toggleDrawer("dg-detail-drawer", true);

  if (!v.series_id) {
    // 未绑定不代表这个框是空的：纯调研节点全靠静态系数说话，方框上只放得下两条
    const st = (node.statics || []);
    box.innerHTML = `
      <div class="py-6 text-center text-[var(--text-muted)] text-xs">
        这个方框还没有绑定指标。${dg.edit ? "" : "进入「编辑布局」后点它即可绑定。"}</div>
      ${st.length ? `<div class="py-3 border-t border-[var(--line)] text-[11px]">
        <div class="text-[10px] font-semibold text-[var(--text-muted)] mb-1">
          静态标注 —— 调研得来的结构系数，<b>不是实测序列</b></div>
        ${st.map((s) => `<div class="flex gap-2 py-0.5">
          <span class="w-20 shrink-0 text-[var(--text-muted)]">${dgEsc(s.label)}</span>
          <span class="flex-1">${dgEsc(s.value)}</span></div>`).join("")}</div>` : ""}`;
    return;
  }
  box.innerHTML = '<div class="py-8 text-center text-[var(--text-muted)] text-xs">加载中…</div>';
  const res = await fetch(`/api/sn/diagram/detail?series_id=${encodeURIComponent(v.series_id)}`);
  if (!res.ok) { box.innerHTML = '<div class="py-8 text-center text-[var(--alert)] text-xs">读取失败</div>'; return; }
  const d = await res.json();
  dg.lastDetail = d;

  const row = (k, val) => val
    ? `<div class="flex gap-2 py-0.5"><span class="w-20 shrink-0 text-[var(--text-muted)]">${dgEsc(k)}</span>
       <span class="flex-1 break-all">${val}</span></div>` : "";
  const dir = dgDir(v.delta != null ? v.delta : v.mom);
  const stateText = { ok: "正常", stale: "断更", no_data: "尚无数据", blocked: "阻断不出数",
                      missing_input: "缺少输入", retired: "指标已停用", unbound: "未绑定" };

  box.innerHTML = `
    <div class="pb-3 border-b border-[var(--line)]">
      <div class="flex items-baseline gap-2">
        <span class="text-2xl font-black tabular">${dgEsc(dgNum(v.value, v.unit))}</span>
        ${v.delta != null ? `<span class="${dir.cls} font-semibold tabular text-sm">
          ${dir.mark} ${dgEsc(dgNum(v.delta, "").replace(/^-/, "−"))}　${dgPct(v.mom) || ""}</span>` : ""}
      </div>
      <div class="text-[11px] text-[var(--text-muted)] mt-1 tabular">
        ${dgEsc(d.frequency)}频 · 数据时点 ${dgEsc(v.as_of || "—")}
        · 状态 <b class="${v.state === "ok" ? "" : "text-[var(--amber)]"}">${stateText[v.state] || v.state}</b>
        ${v.proxy ? ' · <b class="text-[var(--amber)]">代理指标，非本环节实测</b>' : ""}
      </div>
      ${v.note ? `<div class="mt-1.5 text-[11px] text-[var(--amber)]">${dgEsc(v.note)}</div>` : ""}
      ${v.yoy != null ? `<div class="mt-1 text-[11px] tabular">
        同比 <span class="${dgDir(v.yoy).cls} font-semibold">${dgPct(v.yoy)}</span></div>` : ""}
    </div>

    ${(node.statics || []).length ? `<div class="py-3 border-b border-[var(--line)] text-[11px]">
      <div class="text-[10px] font-semibold text-[var(--text-muted)] mb-1">
        静态标注 —— 调研得来的结构系数，<b>不是实测序列</b>，一年动一次</div>
      ${node.statics.map((s) => `<div class="flex gap-2 py-0.5">
        <span class="w-20 shrink-0 text-[var(--text-muted)]">${dgEsc(s.label)}</span>
        <span class="flex-1">${dgEsc(s.value)}</span></div>`).join("")}
    </div>` : ""}

    ${d.history.length > 1 ? `<div class="py-3 border-b border-[var(--line)]">
      <div class="text-[10px] font-semibold text-[var(--text-muted)] mb-1">
        近 ${d.history.length} 期走势（${dgEsc(d.history[0].as_of)} ~ ${dgEsc(d.history[d.history.length - 1].as_of)}）</div>
      ${dgChart(d.history, d.unit, 420, 180)}</div>` : ""}

    <div class="py-3 border-b border-[var(--line)] text-[11px]">
      <div class="text-[10px] font-semibold text-[var(--text-muted)] mb-1">口径</div>
      ${d.caliber.length
        ? d.caliber.map((c) => row(c.label, dgEsc(c.value))).join("")
        : '<div class="text-[var(--text-muted)]">未标注口径维度</div>'}
      ${d.expression ? row("计算式", dgEsc(d.expression)) : ""}
      ${d.tolerance ? row("容差", dgEsc(d.tolerance)) : ""}
    </div>

    <div class="py-3 border-b border-[var(--line)] text-[11px]">
      <div class="text-[10px] font-semibold text-[var(--text-muted)] mb-1">来源与凭证</div>
      ${row("指标代码", dgEsc(d.series_id))}
      ${row("供应商编码", dgEsc(d.vendor_code || ""))}
      ${row("来源", dgEsc(d.source || ""))}
      ${row("取数方式", d.fetch_mode === "auto" ? "自动采集" : "人工录入")}
      ${row("录入人", dgEsc(d.entered_by || ""))}
      ${d.owner ? row("负责人", `<span class="text-[var(--amber)]">${dgEsc(d.owner)}</span>`) : ""}
      ${d.latest_note ? row("入库说明", dgEsc(d.latest_note)) : ""}
      ${d.source_url ? row("凭证", `<a href="${dgEsc(d.source_url)}" target="_blank"
          class="text-[var(--primary)] underline">${dgEsc(d.source_url.slice(0, 60))}</a>`) : ""}
    </div>

    ${d.revisions.length ? `<div class="py-3 border-b border-[var(--line)] text-[11px]">
      <div class="text-[10px] font-semibold text-[var(--amber)] mb-1">
        该序列有 ${d.revisions.length} 处修订 —— 数据商回溯改过数</div>
      ${d.revisions.slice(0, 5).map((r) =>
        `<div class="tabular">${dgEsc(r.as_of)} → ${r.value}（修订 ${r.revision}）</div>`).join("")}
    </div>` : ""}

    ${(d.inputs || []).length ? `<div class="py-3 border-b border-[var(--line)] text-[11px]">
      <div class="text-[10px] font-semibold text-[var(--text-muted)] mb-1">计算输入</div>
      ${d.inputs.map((i) => `<div class="tabular">${dgEsc(i.role)}：${dgEsc(i.series_id)} =
        ${i.value} @ ${dgEsc((i.as_of || "").slice(0, 16).replace("T", " "))}</div>`).join("")}
    </div>` : ""}

    <div class="pt-3 flex flex-wrap gap-1.5 text-[11px]">
      <button type="button" onclick="dgAddCompare('${dgEsc(d.series_id)}','${dgEsc(d.name)}')"
         class="px-2.5 py-1.5 rounded-md border border-[var(--line)] hover:bg-[var(--surface-soft)]">加入对比</button>
      <a href="/sn/indicators#${dgEsc(d.series_id)}"
         class="px-2.5 py-1.5 rounded-md border border-[var(--line)] hover:bg-[var(--surface-soft)]">在指标页查看</a>
      ${d.fetch_mode === "manual" ? `<a href="/sn/entry"
         class="px-2.5 py-1.5 rounded-md border border-[var(--line)] hover:bg-[var(--surface-soft)]">去录入</a>` : ""}
      <button type="button" onclick="dgCopy('${dgEsc(d.series_id)}')"
         class="px-2.5 py-1.5 rounded-md border border-[var(--line)] hover:bg-[var(--surface-soft)]">复制代码</button>
    </div>`;
  dgBindChartHover();
}

function dgCopy(t) {
  navigator.clipboard.writeText(t).then(() => showToast("已复制 " + t), () => showToast("复制失败"));
}

/* ---------- 多序列对比（#2） ---------- */

const DG_COMPARE_COLORS = ["var(--primary)", "var(--amber)", "var(--blue)", "var(--alert)"];

function dgSyncCompareBtn() {
  // 对比篮子原来没有任何常驻入口：加进去之后抽屉一关就找不回来，也看不出里面有几条。
  const btn = document.getElementById("dg-compare-btn");
  if (!btn) return;
  const n = (dg.compare || []).length;
  btn.querySelector("[data-count]").textContent = n ? ` ${n}` : "";
  btn.classList.toggle("bg-[var(--primary-soft)]", n > 0);
  btn.classList.toggle("text-[var(--primary)]", n > 0);
  btn.classList.toggle("border-[var(--primary)]", n > 0);
}

function dgOpenCompare() {
  toggleDrawer("dg-compare-drawer", true);
  dgRenderCompare();
}

function dgAddCompare(seriesId, name) {
  dg.compare = dg.compare || [];
  if (dg.compare.some((c) => c.series_id === seriesId)) {
    showToast("已在对比里");
    dgOpenCompare();
    return;
  }
  if (dg.compare.length >= 4) {
    showToast("最多对比 4 条，先移除一条");
    dgOpenCompare();
    return;
  }
  dg.compare.push({ series_id: seriesId, name });
  dgSyncCompareBtn();
  dgRenderCompare();
  toggleDrawer("dg-compare-drawer", true);
}

function dgDropCompare(seriesId) {
  dg.compare = (dg.compare || []).filter((c) => c.series_id !== seriesId);
  dgSyncCompareBtn();
  dgRenderCompare();
}

function dgClearCompare() {
  dg.compare = [];
  dgSyncCompareBtn();
  dgRenderCompare();
}

function dgCompareMode(mode) {
  dg.compareMode = mode;
  dgRenderCompare();
}

function dgCompareTabs(mode, canDiff, why) {
  const tab = (id, text, on, enabled) => `<button type="button"
    ${enabled ? `onclick="dgCompareMode('${id}')"` : "disabled"}
    title="${dgEsc(enabled ? "" : why)}"
    class="px-2.5 py-1 rounded-md text-[11px] font-semibold ${on
      ? "bg-[var(--primary)] text-white"
      : enabled ? "border border-[var(--line)] hover:bg-[var(--surface-soft)]"
                : "border border-[var(--line)] opacity-40 cursor-not-allowed"}">${text}</button>`;
  return `<div class="flex items-center gap-1.5 pb-2">
    ${tab("index", "归一化趋势", mode !== "diff", true)}
    ${tab("diff", "差值 A − B", mode === "diff", canDiff)}
    <button type="button" onclick="dgClearCompare()"
      class="ml-auto text-[11px] text-[var(--text-muted)] hover:text-[var(--alert)]">清空</button>
  </div>`;
}

async function dgRenderCompare() {
  const box = document.getElementById("dg-compare-body");
  const list = dg.compare || [];
  if (!list.length) {
    box.innerHTML = `<div class="py-8 text-center text-xs text-[var(--text-muted)]">
      对比篮子是空的。<br class="mb-1">点任意方框 → 详情抽屉底部「加入对比」。</div>`;
    return;
  }
  box.innerHTML = '<div class="py-8 text-center text-xs text-[var(--text-muted)]">加载中…</div>';
  const loaded = [];
  for (const c of list) {
    const res = await fetch(`/api/sn/diagram/detail?series_id=${encodeURIComponent(c.series_id)}`);
    if (res.ok) {
      const d = await res.json();
      if (d.history.length > 1) loaded.push({ ...c, unit: d.unit, history: d.history });
    }
  }
  if (!loaded.length) { box.innerHTML = '<div class="py-8 text-center text-xs">没有可比较的数据</div>'; return; }

  // 差值只在**两条、且单位相同**时允许。单位不同的相减出来的数没有含义，而它看起来
  // 完全正常——这正是 FR-5.2 要防的东西，所以在按钮上就禁掉，不是算完再报错。
  const canDiff = loaded.length === 2 && loaded[0].unit === loaded[1].unit;
  const why = loaded.length !== 2
    ? "差值只能在两条序列之间做，现在有 " + loaded.length + " 条"
    : `单位不同（${loaded[0].unit} / ${loaded[1].unit}），相减没有含义`;
  const mode = (dg.compareMode === "diff" && canDiff) ? "diff" : "index";
  const tabs = dgCompareTabs(mode, canDiff, why);

  const common = loaded.map((s) => new Set(s.history.map((p) => p.as_of)))
    .reduce((a, b) => new Set([...a].filter((x) => b.has(x))));
  const days = [...common].sort();
  if (days.length < 2) {
    box.innerHTML = tabs + `<div class="py-6 px-3 text-xs text-[var(--amber)]">
      这几条序列没有足够的共同时点（频率不同时常见）——无法在同一时间轴上比较。</div>`;
    return;
  }

  const w = 420, h = 200, PAD = { l: 48, r: 12, t: 12, b: 26 };
  const iw = w - PAD.l - PAD.r, ih = h - PAD.t - PAD.b;
  const X = (i) => PAD.l + (i / (days.length - 1)) * iw;
  const byDayOf = (s) => Object.fromEntries(s.history.map((p) => [p.as_of, p.value]));

  let series, head, foot, zero = null;
  if (mode === "diff") {
    // 差值图画的是绝对量，纵轴就是真实数值，还要画出 0 线——差值的符号翻转
    // （从升水变贴水、从缺口变过剩）才是要看的东西。
    const [a, b] = loaded.map(byDayOf);
    series = [{ name: `${loaded[0].name} − ${loaded[1].name}`, unit: loaded[0].unit,
                color: "var(--primary)", series_id: null,
                pts: days.map((d) => ({ as_of: d, value: a[d] - b[d] })) }];
    head = `<div class="px-1 pb-2 text-[11px]">
      <b>差值</b>：${dgEsc(loaded[0].name)} − ${dgEsc(loaded[1].name)}，
      单位 ${dgEsc(loaded[0].unit)}。纵轴是真实数值，虚线为 0。</div>`;
  } else {
    series = loaded.map((s, i) => {
      const byDay = byDayOf(s);
      const base = byDay[days[0]] || 1;
      return { ...s, color: DG_COMPARE_COLORS[i % 4],
               pts: days.map((d) => ({ as_of: d, value: (byDay[d] / base) * 100 })) };
    });
    head = `<div class="px-1 pb-2 text-[11px] text-[var(--amber)]">
      已归一化：各序列以 ${dgEsc(days[0])} 为 100。<b>纵轴不是绝对值</b>，只能比趋势，不能比大小。</div>`;
  }

  const all = series.flatMap((s) => s.pts.map((p) => p.value));
  let lo = Math.min(...all), hi = Math.max(...all);
  if (mode === "diff") { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  if (lo === hi) { lo -= 1; hi += 1; }
  const padv = (hi - lo) * 0.08;
  lo -= padv; hi += padv;
  const Y = (v) => PAD.t + ih - ((v - lo) / (hi - lo)) * ih;
  if (mode === "diff") {
    zero = `<line x1="${PAD.l}" y1="${Y(0).toFixed(1)}" x2="${w - PAD.r}" y2="${Y(0).toFixed(1)}"
      stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="4 3" opacity="0.9"/>`;
  }
  const grid = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const v = lo + (hi - lo) * f;
    return `<line x1="${PAD.l}" y1="${Y(v).toFixed(1)}" x2="${w - PAD.r}" y2="${Y(v).toFixed(1)}"
      stroke="var(--line)" stroke-width="0.8"/>
      <text x="${PAD.l - 5}" y="${(Y(v) + 3).toFixed(1)}" text-anchor="end" font-size="9"
        fill="var(--text-muted)" class="tabular">${dgEsc(dgFmt(v))}</text>`;
  }).join("");
  const every = Math.max(1, Math.ceil(days.length / 4));
  const at = days.map((d, i) => i).filter((i) => i % every === 0 || i === days.length - 1);
  const fmtX = dgAxisDate(at.map((i) => days[i]));
  const xlab = at.map((i) => `<text x="${X(i).toFixed(1)}" y="${h - 8}" text-anchor="middle"
      font-size="9" fill="var(--text-muted)" class="tabular">${dgEsc(fmtX(days[i]))}</text>`).join("");
  const lines = series.map((s) => `<path d="${s.pts.map((p, i) =>
    `${i ? "L" : "M"} ${X(i).toFixed(1)} ${Y(p.value).toFixed(1)}`).join(" ")}"
    fill="none" stroke="${s.color}" stroke-width="1.8"/>`).join("");

  // 图例始终列出篮子里的全部序列——差值模式下也要能把其中一条摘掉
  const legend = loaded.map((s, i) => {
    const color = mode === "diff" ? (i ? "var(--text-muted)" : "var(--primary)")
                                  : DG_COMPARE_COLORS[i % 4];
    // 归一化模式右侧给区间涨跌幅（图上读不出来）；差值模式给最新原值（图上画的是差，
    // 两条各自多少反而看不见了）。
    let tail;
    if (mode === "diff") {
      tail = `<span class="tabular text-[var(--text-muted)]">${dgEsc(dgFmt(
        s.history[s.history.length - 1].value))}</span>`;
    } else {
      const pct = series[i].pts[series[i].pts.length - 1].value - 100;
      const d = dgDir(pct);
      tail = `<span class="${d.cls} tabular font-semibold">${d.mark}${pct.toFixed(1)}%</span>`;
    }
    return `<div class="flex items-center gap-2">
      <i style="width:10px;height:3px;background:${color};display:inline-block"></i>
      <span class="flex-1 truncate">${mode === "diff" ? (i ? "− " : "") : ""}${dgEsc(s.name)}
        <span class="text-[var(--text-muted)]">（${dgEsc(s.unit)}）</span></span>
      ${tail}
      <button type="button" onclick="dgDropCompare('${dgEsc(s.series_id)}')" title="从对比中移除"
        class="text-[var(--text-muted)] hover:text-[var(--alert)]">&times;</button></div>`;
  }).join("");

  if (mode === "diff") {
    const pts = series[0].pts;
    const last = pts[pts.length - 1].value, first = pts[0].value;
    const flips = pts.filter((p, i) => i && Math.sign(p.value) !== Math.sign(pts[i - 1].value)).length;
    const d = dgDir(last - first);
    foot = `<div class="mt-2 text-[11px] tabular">
      最新差值 <b>${dgEsc(dgNum(last, loaded[0].unit))}</b>
      <span class="${d.cls}">${d.mark} 较期初 ${dgEsc(dgNum(last - first, ""))}</span></div>
      ${flips ? `<div class="mt-1 text-[11px] text-[var(--amber)]">
        区间内正负号翻转 ${flips} 次 —— 方向变过，不是单纯变大变小</div>` : ""}`;
  } else {
    // 归一化模式的“涨跌幅”已经写在每条图例右侧，这里不再重复一遍
    foot = "";
  }

  box.innerHTML = tabs + head + `
    <svg viewBox="0 0 ${w} ${h}" class="w-full" style="height:${h}px">${grid}${xlab}${zero || ""}
      <line x1="${PAD.l}" y1="${PAD.t}" x2="${PAD.l}" y2="${PAD.t + ih}"
            stroke="var(--line-strong)" stroke-width="1"/>
      <line x1="${PAD.l}" y1="${PAD.t + ih}" x2="${w - PAD.r}" y2="${PAD.t + ih}"
            stroke="var(--line-strong)" stroke-width="1"/>${lines}</svg>
    ${foot}
    <div class="mt-2 pt-2 border-t border-[var(--line)] space-y-1 text-[11px]">${legend}</div>
    ${!canDiff && loaded.length === 2 ? `<div class="mt-2 text-[10px] text-[var(--amber)]">
      差值不可用：${dgEsc(why)}。换算口径不该在图上临时拍——要么改绑同口径的指标，
      要么登记一条派生指标由公式层带守卫地算。</div>` : ""}
    <div class="mt-2 text-[10px] text-[var(--text-muted)]">
      共同时点 ${days.length} 个（${dgEsc(days[0])} ~ ${dgEsc(days[days.length - 1])}）</div>`;
}

/* ---------- 判断视角与交叉校验（#5 #6） ---------- */

function dgToggleJudgment() {
  dg.judgmentView = !dg.judgmentView;
  const btn = document.getElementById("dg-judge-btn");
  btn.classList.toggle("bg-[var(--primary-soft)]", dg.judgmentView);
  btn.classList.toggle("text-[var(--primary)]", dg.judgmentView);
  const n = Object.values(dg.values).filter((v) => (v.roles || []).length).length;
  const j = dg.judgment || {};
  dgRender();

  // 必须说出依据的是哪一版判断：重点关注跟着判断走，判断改版这些高亮就会变。
  // 不写版本号，看图的人无从知道自己看的是不是最新那一版关心的环节。
  const bar = document.getElementById("dg-judge-bar");
  if (bar) {
    bar.classList.toggle("hidden", !dg.judgmentView);
    if (dg.judgmentView) {
      bar.innerHTML = j.version
        ? `重点关注来自<b>判断 v${j.version}</b>（${dgEsc(j.status)}${
            j.author ? " · " + dgEsc(j.author) : ""}${
            j.written_at ? " · 写于 " + dgEsc(j.written_at) : ""}）——
           ${n} 个环节被它引用，其余淡出。<a href="/sn/judgment"
           class="underline font-semibold">改判断</a>后这里会跟着变。`
        : `当前品种还没有判断，因此没有任何环节被标为重点关注。`;
    }
  }
  showToast(dg.judgmentView
    ? (j.version ? `判断 v${j.version}：${n} 个环节被引用，其余淡出`
                 : "还没有判断，没有重点关注")
    : "已回到全图");
}

async function dgOpenCrosscheck() {
  const box = document.getElementById("dg-check-body");
  box.innerHTML = '<div class="py-8 text-center text-xs text-[var(--text-muted)]">正在核对…</div>';
  toggleDrawer("dg-check-drawer", true);
  const res = await fetch("/api/sn/diagram/crosscheck");
  const data = res.ok ? await res.json() : { checks: [] };
  if (!data.checks.length) {
    box.innerHTML = '<div class="py-8 text-center text-xs text-[var(--text-muted)]">还没有配置校验项</div>';
    return;
  }
  box.innerHTML = data.checks.map((c) => {
    const bad = c.state !== "ok";
    return `
    <div class="py-3 border-b border-[var(--line)]">
      <div class="flex items-baseline gap-2">
        <b class="text-xs">${dgEsc(c.label)}</b>
        <span class="text-[10px] px-1.5 py-0.5 rounded ${bad
          ? "bg-[var(--amber-soft)] text-[var(--amber)]" : "bg-[var(--primary-soft)] text-[var(--primary)]"}">
          ${bad ? "超出容差" : "一致"}</span>
      </div>
      <div class="text-[11px] mt-1 ${bad ? "text-[var(--amber)]" : "text-[var(--text-muted)]"}">${dgEsc(c.summary)}</div>
      ${c.note ? `<div class="text-[10px] text-[var(--text-muted)] mt-0.5">${dgEsc(c.note)}</div>` : ""}
      ${(c.points || []).length ? `<table class="mt-1.5 w-full text-[10px] tabular">
        <tr class="text-[var(--text-muted)]"><td>时点</td><td class="text-right">${dgEsc(c.a_name)}</td>
          <td class="text-right">${dgEsc(c.b_name)}</td><td class="text-right">相对差</td></tr>
        ${c.points.slice(0, 6).map((p) => `<tr>
          <td>${dgEsc(p.as_of)}</td><td class="text-right">${dgFmt(p.a)}</td>
          <td class="text-right">${dgFmt(p.b)}</td>
          <td class="text-right ${Math.abs(p.rel) > (c.tolerance_pct || 5)
            ? "text-[var(--amber)] font-semibold" : ""}">${p.rel == null ? "—" : p.rel.toFixed(1) + "%"}</td>
        </tr>`).join("")}</table>` : ""}
    </div>`;
  }).join("") + `<div class="pt-3 text-[10px] text-[var(--text-muted)]">
      差异本身不是错误——两家口径不同很正常。要盯的是<b>差异突然变化</b>，那说明有一方改过数或换了口径。</div>`;
}

/* ---------- 分组抽屉 ----------
 *
 * 分组框原来在看图模式下点了没反应，像张背景纸。但它是个实体：有成员、有合计、
 * 有自己的层级位置。这个抽屉回答三件事：
 *   这个环节里有什么、整体怎么样、哪一项在拖后腿。
 */

function dgOpenGroup(gid) {
  const g = dg.layout.nodes.find((n) => n.id === gid);
  if (!g) return;
  dg.activeGroup = gid;
  document.getElementById("dg-group-title").textContent = g.label;
  document.getElementById("dg-group-trail").textContent = dgTrail(gid) || "顶层分组";
  toggleDrawer("dg-group-drawer", true);

  const kids = dg.layout.nodes.filter((n) => n.parent === gid);
  const subs = kids.filter(dgIsGroup);
  const leaves = kids.filter((n) => !dgIsGroup(n));
  const v = dg.values[gid] || {};
  const vals = leaves.map((n) => ({ n, v: dg.values[n.id] || {} }));
  const withVal = vals.filter((x) => x.v.value != null);

  // 合计/占比
  const aggOp = (g.binding || {}).kind === "agg" ? g.binding.op : "";
  const picked = (g.binding || {}).members || null;
  let aggBox = "";
  if (aggOp) {
    const bad = v.state === "blocked" || v.state === "no_data" || v.state === "unbound";
    aggBox = `
      <div class="rounded-md border ${bad ? "border-[var(--amber)]/40 bg-[var(--amber-soft)]"
                                          : "border-[var(--primary)]/30 bg-[var(--primary-soft)]"} p-3">
        <div class="text-[10px] font-semibold text-[var(--text-muted)]">
          ${aggOp === "share" ? "占比" : "合计"}（本分组的直接子节点）</div>
        ${bad ? `<div class="text-sm font-bold text-[var(--amber)] mt-1">不出数</div>`
              : `<div class="text-xl font-black tabular mt-1">${dgEsc(dgNum(v.value, v.unit))}</div>`}
        <div class="text-[11px] text-[var(--text-muted)] mt-1">${dgEsc(v.note || "")}</div>
        ${(v.parts || []).length ? `<div class="mt-2 space-y-1">${v.parts.map((p) => `
          <div class="flex items-center gap-2 text-[11px]">
            <span class="w-28 shrink-0 truncate">${dgEsc(p.label)}</span>
            <span class="flex-1 h-2 rounded bg-[var(--surface)] overflow-hidden">
              <i style="display:block;height:100%;width:${Math.max(1, p.pct).toFixed(1)}%;
                 background:var(--primary);opacity:.65"></i></span>
            <span class="tabular w-12 text-right font-semibold">${p.pct.toFixed(1)}%</span>
          </div>`).join("")}</div>` : ""}
      </div>`;
  }

  // 成员一览：按本期变化幅度排序——"哪一项在动"比"有哪些项"更值得先看到
  const ranked = withVal.slice().sort((a, b) =>
    Math.abs(b.v.mom ?? 0) - Math.abs(a.v.mom ?? 0));
  const stateText = { ok: "正常", stale: "断更", no_data: "尚无数据", blocked: "阻断",
                      missing_input: "缺少输入", retired: "已停用", unbound: "未绑定" };
  const rows = vals.map(({ n, v: x }) => {
    const d = dgDir(x.delta != null ? x.delta : x.mom);
    return `<button type="button" onclick="dgOpenDetail('${dgEsc(n.id)}')"
      class="w-full text-left flex items-baseline gap-2 py-1.5 px-1 rounded hover:bg-[var(--surface-soft)]">
      <span class="flex-1 truncate text-[11px] font-medium">${dgEsc(n.label)}</span>
      ${x.value != null
        ? `<span class="tabular text-[11px]">${dgEsc(dgNum(x.value, x.unit))}</span>
           <span class="${d.cls} tabular text-[10px] w-14 text-right">${
             dgPct(x.mom) != null ? d.mark + dgPct(x.mom) : ""}</span>`
        : `<span class="text-[10px] text-[var(--text-muted)]">${
             stateText[x.state] || "未绑定"}</span>`}
    </button>`;
  }).join("");

  const problems = vals.filter((x) => x.v.state && x.v.state !== "ok");
  document.getElementById("dg-group-body").innerHTML = `
    ${aggBox}
    <div class="mt-3 grid grid-cols-3 gap-2 text-center">
      ${[["直接节点", leaves.length], ["子分组", subs.length], ["有数据", withVal.length]]
        .map(([k, n]) => `<div class="rounded-md border border-[var(--line)] py-2">
          <div class="text-lg font-black tabular">${n}</div>
          <div class="text-[10px] text-[var(--text-muted)]">${k}</div></div>`).join("")}
    </div>

    ${problems.length ? `<div class="mt-3 px-2.5 py-2 rounded-md bg-[var(--amber-soft)]
      text-[var(--amber)] text-[11px]">
      ${problems.length} 项不是「正常」：${problems.map((x) =>
        `${dgEsc(x.n.label)}（${stateText[x.v.state] || x.v.state}）`).join("、")}
      </div>` : ""}

    ${ranked.length ? `<div class="mt-3">
      <div class="text-[10px] font-semibold text-[var(--text-muted)] mb-1">
        本期动得最大的</div>
      ${ranked.slice(0, 3).map(({ n, v: x }) => {
        const d = dgDir(x.mom);
        return `<div class="flex items-baseline gap-2 text-[11px] py-0.5">
          <span class="flex-1 truncate">${dgEsc(n.label)}</span>
          <span class="${d.cls} tabular font-semibold">${d.mark} ${dgPct(x.mom) || "—"}</span></div>`;
      }).join("")}</div>` : ""}

    <div class="mt-3 pt-3 border-t border-[var(--line)]">
      <div class="text-[10px] font-semibold text-[var(--text-muted)] mb-1">成员（点开看凭证）</div>
      ${rows || '<div class="py-3 text-center text-[11px] text-[var(--text-muted)]">这个分组里还没有节点</div>'}
      ${subs.length ? `<div class="mt-2 pt-2 border-t border-[var(--line)]">
        <div class="text-[10px] font-semibold text-[var(--text-muted)] mb-1">子分组</div>
        ${subs.map((x) => `<button type="button" onclick="dgOpenGroup('${dgEsc(x.id)}')"
          class="w-full text-left py-1 px-1 rounded text-[11px] hover:bg-[var(--surface-soft)]">
          ${dgEsc(x.label)} ›</button>`).join("")}</div>` : ""}
    </div>

    <div class="mt-3 pt-3 border-t border-[var(--line)]">
      <div class="text-[10px] font-semibold text-[var(--text-muted)] mb-1.5">分组公式</div>
      <div class="flex items-center gap-1.5 text-[11px]">
        ${[["", "不计算"], ["sum", "合计 Σ"], ["share", "占比 %"]].map(([op, label]) => `
          <button type="button" onclick="dgSetAgg('${dgEsc(gid)}','${op}')"
            class="px-2.5 py-1.5 rounded-md font-semibold ${aggOp === op
              ? "bg-[var(--primary)] text-white"
              : "border border-[var(--line)] hover:bg-[var(--surface-soft)]"}">${label}</button>`).join("")}
      </div>
      ${aggOp ? `
      <div class="mt-2">
        <div class="flex items-baseline gap-2 text-[10px] text-[var(--text-muted)] mb-1">
          <span class="font-semibold">参与项</span>
          <span>${picked ? `手选 ${picked.length} 项` : "全部直接子节点"}</span>
          <button type="button" onclick="dgAggMembers('${dgEsc(gid)}', null)"
            class="ml-auto hover:text-[var(--primary)] ${picked ? "" : "opacity-40"}">恢复全选</button>
        </div>
        ${leaves.map((n) => {
          const on = !picked || picked.includes(n.id);
          const x = dg.values[n.id] || {};
          return `<label class="flex items-center gap-2 py-1 px-1 rounded text-[11px]
            hover:bg-[var(--surface-soft)] cursor-pointer">
            <input type="checkbox" ${on ? "checked" : ""}
              onchange="dgToggleMember('${dgEsc(gid)}','${dgEsc(n.id)}')">
            <span class="flex-1 truncate ${on ? "" : "text-[var(--text-muted)] line-through"}">${dgEsc(n.label)}</span>
            <span class="text-[10px] text-[var(--text-muted)] tabular">${dgEsc(x.unit || "—")}</span>
          </label>`;
        }).join("")}
      </div>` : ""}
      <p class="text-[10px] text-[var(--text-muted)] mt-1.5 leading-relaxed">
        只对<b>直接子节点</b>生效，默认全部；一个分组里常混着量和价（矿端既有产量也有
        加工费），这时手选参与项。单位或频率不一致时<b>不出数</b>并说明原因 ——
        实物吨和金属吨差 2–3 倍，加出来的数看着完全正常，那比留空危险得多。
        Markdown 写成 <code>## 分组名 &lt;!-- agg: sum(甲, 乙) --&gt;</code>，
        不写括号即全部。</p>
    </div>`;
}

function dgToggleMember(gid, nid) {
  const g = dg.layout.nodes.find((n) => n.id === gid);
  if (!g || (g.binding || {}).kind !== "agg") return;
  const leaves = dg.layout.nodes
    .filter((n) => n.parent === gid && !dgIsGroup(n)).map((n) => n.id);
  // members 缺省＝全部。第一次取消勾选时先落成显式全集，再把这一项去掉。
  const cur = g.binding.members ? g.binding.members.slice() : leaves.slice();
  const at = cur.indexOf(nid);
  if (at >= 0) cur.splice(at, 1);
  else cur.push(nid);
  dgAggMembers(gid, cur.length === leaves.length ? null : cur);
}

function dgAggMembers(gid, members) {
  const g = dg.layout.nodes.find((n) => n.id === gid);
  if (!g || (g.binding || {}).kind !== "agg") return;
  dgPushUndo();
  if (members && members.length) g.binding.members = members;
  else delete g.binding.members;    // 缺省即全部，不存一个等价的全集
  dg.dirty = true;
  dgSyncSave();
  dgLoad().then(() => dgOpenGroup(gid));
}

function dgSetAgg(gid, op) {
  const g = dg.layout.nodes.find((n) => n.id === gid);
  if (!g) return;
  dgPushUndo();
  g.binding = op ? { kind: "agg", op } : { kind: "none" };
  dg.dirty = true;
  dgSyncSave();
  dgLoad().then(() => dgOpenGroup(gid));
}

function dgRenameGroup() {
  const g = dg.layout.nodes.find((n) => n.id === dg.activeGroup);
  if (!g) return;
  const name = (prompt("分组名称", g.label) || "").trim();
  if (!name || name === g.label) return;
  dgPushUndo();
  g.label = name;
  dg.dirty = true;
  dgSyncSave();
  dgRender();
  dgOpenGroup(g.id);
}

/* ---------- 自动排布 ----------
 *
 * 新加的节点落在视口角上、拖进分组的节点停在手松开的地方——两者都会和已有的框
 * 叠在一起。叠了就得手动一个个挪开，而挪的过程中又可能把别的挤重叠。
 *
 * 排布只做一件事：把一个分组的**直接子元素**摆成不重叠的网格，顺序沿用它们原来的
 * 上下左右关系——研究员摆的次序是他的表达，自动排布可以对齐，但不该重排。
 */

const DG_PAD = 12, DG_HEAD = 26, DG_GAPX = 20, DG_GAPY = 14, DG_INDENT = 18;

function dgMoveTree(node, dx, dy) {
  for (const n of dgSubtree(node.id)) { n.x += dx; n.y += dy; }
}

function dgContainedIn(gid) {
  // 同组内一条 A→B 的连线表示"B 是 A 的一部分"，排布时 B 紧跟 A 并缩进
  const ids = new Set(dg.layout.nodes.filter((n) => n.parent === gid).map((n) => n.id));
  const of = {};
  for (const e of dg.layout.edges || []) {
    if (ids.has(e.from) && ids.has(e.to)) of[e.to] = e.from;
  }
  return of;
}

function dgArrange(gid) {
  const g = dg.layout.nodes.find((n) => n.id === gid);
  if (!g) return 0;
  const kids = dg.layout.nodes.filter((n) => n.parent === gid);
  if (!kids.length) return 0;

  const byXY = (a, b) => (a.y - b.y) || (a.x - b.x);
  const subs = kids.filter(dgIsGroup).sort(byXY);
  const leaves = kids.filter((n) => !dgIsGroup(n)).sort(byXY);

  for (const s of subs) dgArrange(s.id);       // 内层先各自排好，外层才知道它多大

  let x = g.x + DG_PAD;
  const top = g.y + DG_HEAD;
  for (const s of subs) {                       // 子分组横向并排
    dgMoveTree(s, x - s.x, top - s.y);
    x += s.w + DG_GAPX;
  }

  if (leaves.length) {
    // 把"被包含的"排到它所属那一项后面，其余保持原有上下顺序
    const owner = dgContainedIn(gid);
    const order = [];
    const seen = new Set();
    const push = (n) => {
      if (seen.has(n.id)) return;
      seen.add(n.id);
      order.push(n);
      for (const k of leaves) if (owner[k.id] === n.id) push(k);
    };
    for (const n of leaves) if (!owner[n.id]) push(n);
    for (const n of leaves) push(n);            // owner 已被删掉的孤儿也要排进去

    const cellW = Math.max(...leaves.map((n) => n.w));
    const cellH = Math.max(...leaves.map((n) => n.h));
    // 列数按分组当前宽度算：框宽就多排几列，而不是一味拉长
    const cols = Math.max(1, Math.floor((g.w - DG_PAD * 2 + DG_GAPX) / (cellW + DG_GAPX)));
    const rows = Math.ceil(order.length / cols);
    order.forEach((n, i) => {
      const col = Math.floor(i / rows), row = i % rows;   // 先竖后横，和种子布局一致
      const indent = owner[n.id] ? DG_INDENT : 0;
      n.x = x + col * (cellW + DG_GAPX) + indent;
      n.y = top + row * (cellH + DG_GAPY);
      if (indent && n.w > cellW - indent) n.w = cellW - indent;
    });
    x += cols * (cellW + DG_GAPX) - DG_GAPX;
  }

  dgFitGroup(g);
  return kids.length;
}

function dgFitGroup(g) {
  const kids = dg.layout.nodes.filter((n) => n.parent === g.id);
  if (!kids.length) return;
  const x = Math.min(...kids.map((k) => k.x)) - DG_PAD;
  const y = Math.min(...kids.map((k) => k.y)) - DG_HEAD;
  Object.assign(g, {
    x, y,
    w: Math.max(...kids.map((k) => k.x + k.w)) + DG_PAD - x,
    h: Math.max(...kids.map((k) => k.y + k.h)) + DG_PAD - y,
  });
  // 父框要跟着重新贴合，否则子分组长大之后会顶出去
  const parent = dg.layout.nodes.find((n) => n.id === g.parent);
  if (parent) dgFitGroup(parent);
}

function dgAutoArrange(...gids) {
  const done = new Set();
  let n = 0;
  for (const gid of gids) {
    if (!gid || done.has(gid)) continue;
    done.add(gid);
    n += dgArrange(gid);
  }
  if (n) { dg.dirty = true; dgSyncSave(); dgRender(); }
  return n;
}
