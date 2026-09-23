"""产业图与 Markdown 大纲的互转（方案 08 §9）。

研究员手里已经有维护好的结构——大纲、文档里的层级列表。在画布上重拖一遍是重复劳动，
而且改结构时在文本里改比拖方框快得多。所以这条通道是**双向**的。

格式：

    # 锡产业结构
    ## 供给端
    - 国产锡精矿 [占比 32%] [年体量 5.8–6.5 万金属吨] <!-- bind: MYSTEEL.ID01590150 -->
      - 云南
    ## 精锡
    - 国内产量

规则：
- `##` 一级标题 → 分组（画布上的分组框），标题文字即分组名
- 列表项 → 节点；缩进表示父子，父子之间自动连线
- `[键 值]` → 静态标注（调研系数，不入事实层）
- `<!-- bind: X -->` → 绑定的 series_id / formula_id；**导出时写回，所以导出再导入无损**

**导入给不了绑定**：研究员的大纲里只有名字。没有 `bind` 注释的节点一律 `binding: none`，
交给「建议参考」去补（方案 08 §8）——这两个功能是配套的。
"""

import re

BIND = re.compile(r"<!--\s*bind:\s*([A-Za-z0-9_.一-鿿-]+)\s*-->")
STATIC = re.compile(r"\[([^\[\]]+?)\s+([^\[\]]+?)\]")
ITEM = re.compile(r"^(\s*)[-*+]\s+(.*)$")
HEAD = re.compile(r"^(#{1,4})\s+(.+?)\s*$")

# 画布坐标：一列分组、组内节点按缩进分列
COL_W, ROW_H, PAD = 164, 80, 22
NODE_W, NODE_H = 140, 66


class MarkdownError(ValueError):
    pass


def _slug(text: str, used: set[str], prefix: str) -> str:
    base = re.sub(r"[^A-Za-z0-9一-鿿]+", "_", text).strip("_")[:24] or prefix
    slug, i = base, 1
    while slug in used:
        i += 1
        slug = f"{base}_{i}"
    used.add(slug)
    return slug


def parse(text: str) -> dict:
    """Markdown 大纲 → 布局。认不出结构就明确报错，不猜。

    `##` 是一级分组，`###` 及以下是它的子分组——层级靠井号数表达，与 Markdown 的
    常识一致，研究员不用学新语法。
    """
    groups: list[dict] = []
    current: dict | None = None
    title = None
    used: set[str] = set()

    for raw in text.splitlines():
        if not raw.strip():
            continue
        if (m := HEAD.match(raw)):
            level, label = len(m.group(1)), m.group(2)
            if level == 1 and title is None:
                title = label
                continue
            current = {"label": label, "level": level, "items": []}
            groups.append(current)
            continue
        if (m := ITEM.match(raw)):
            if current is None:  # 没有分组标题时给一个默认组，不因为格式不标准就整份拒收
                current = {"label": "未分组", "level": 2, "items": []}
                groups.append(current)
            indent, body = len(m.group(1).expandtabs(4)), m.group(2)
            bind = BIND.search(body)
            body = BIND.sub("", body)
            statics = [{"label": k.strip(), "value": v.strip()} for k, v in STATIC.findall(body)]
            label = STATIC.sub("", body).strip()
            if not label:
                raise MarkdownError(f"这一行没有节点名称：{raw.strip()}")
            current["items"].append({"indent": indent, "label": label,
                                     "bind": bind.group(1) if bind else None, "statics": statics})
            continue

    if not any(g["items"] for g in groups):
        raise MarkdownError("没有解析出任何节点：每个环节写成一个列表项（`- 名称`），"
                            "用 `## 分组名` 分段")

    nodes: list[dict] = []
    edges: list[dict] = []
    x = PAD
    # 父组 id 按层级维护：遇到 ### 时挂到最近的 ##
    ancestors: dict[int, str] = {}
    pending: list[dict] = []  # 只有标题没有列表项的分组（纯容器）
    for g in groups:
        gid = _slug(g["label"], used, "group")
        g["gid"] = gid
        parent = ancestors.get(g["level"] - 1)
        g["parent"] = parent
        ancestors[g["level"]] = gid
        for deeper in [k for k in ancestors if k > g["level"]]:
            ancestors.pop(deeper)
        if not g["items"]:
            pending.append(g)
            continue
        # 组内按缩进分列，同缩进的往下排
        levels = sorted({it["indent"] for it in g["items"]})
        level_of = {v: i for i, v in enumerate(levels)}
        rows: dict[int, int] = {}
        stack: dict[int, str] = {}
        group_nodes = []
        row_of: dict[str, int] = {}
        for item in g["items"]:
            col = level_of[item["indent"]]
            # 子节点不能排到父节点上方，否则连线倒着走，看图的人会以为物料倒流
            floor = row_of.get(stack.get(col - 1, ""), 0) if col else 0
            row = max(rows.get(col, 0), floor)
            rows[col] = row + 1
            nid = _slug(item["label"], used, "node")
            binding = {"kind": "none"}
            if item["bind"]:
                binding = ({"kind": "derived", "formula_id": item["bind"]}
                           if item["bind"].isupper() and "." not in item["bind"]
                           else {"kind": "series", "series_id": item["bind"]})
            node = {"id": nid, "label": item["label"], "parent": gid,
                    "x": x + col * COL_W, "y": PAD + 46 + row * ROW_H,
                    "w": NODE_W, "h": NODE_H, "binding": binding, "statics": item["statics"]}
            nodes.append(node)
            group_nodes.append(node)
            stack[col] = nid
            row_of[nid] = row
            if col > 0 and (col - 1) in stack:
                edges.append({"from": stack[col - 1], "to": nid})
        width = (max(level_of.values()) + 1) * COL_W + PAD
        height = max(rows.values()) * ROW_H + 60
        nodes.insert(len(nodes) - len(group_nodes),
                     {"id": gid, "kind": "group", "label": g["label"], "parent": g["parent"],
                      "x": x - 14, "y": PAD, "w": width, "h": height,
                      "binding": {"kind": "none"}, "statics": []})
        x += width + PAD

    # 纯容器组（只有标题、下面全是子组）包住它的子组
    boxes = {n["id"]: n for n in nodes if n.get("kind") == "group"}
    for g in reversed(pending):
        kids = [b for b in boxes.values() if b.get("parent") == g["gid"]]
        if not kids:
            continue
        box = {"id": g["gid"], "kind": "group", "label": g["label"], "parent": g["parent"],
               "x": min(k["x"] for k in kids) - 14,
               "y": min(k["y"] for k in kids) - 34,
               "w": max(k["x"] + k["w"] for k in kids) - min(k["x"] for k in kids) + 28,
               "h": max(k["y"] + k["h"] for k in kids) - min(k["y"] for k in kids) + 48,
               "binding": {"kind": "none"}, "statics": []}
        nodes.insert(0, box)
        boxes[g["gid"]] = box

    return {"name": title or "导入的结构图", "nodes": nodes, "edges": edges}


def dump(layout: dict, name: str = "产业结构") -> str:
    """布局 → Markdown。绑定写成注释，所以导出再导入无损。"""
    nodes = layout.get("nodes", [])
    groups = [n for n in nodes if n.get("kind") == "group"]
    edges = layout.get("edges") or []
    parent = {e["to"]: e["from"] for e in edges}

    lines = [f"# {name}", ""]
    plain = [n for n in nodes if n.get("kind") != "group"]
    by_id = {n["id"]: n for n in plain}
    placed: set[str] = set()

    children: dict[str, list[str]] = {}
    for child, father in parent.items():
        if child in by_id and father in by_id:
            children.setdefault(father, []).append(child)

    def emit(node: dict, level: int) -> None:
        """按树输出。同层排在一起会破坏父子相邻，导回来就挂错了爹。"""
        if node["id"] in placed:
            return
        placed.add(node["id"])
        bits = "".join(f" [{s['label']} {s['value']}]" for s in node.get("statics", []))
        b = node.get("binding") or {}
        ref = b.get("series_id") or b.get("formula_id")
        tail = f" <!-- bind: {ref} -->" if ref else ""
        lines.append(f"{'  ' * level}- {node['label']}{bits}{tail}")
        for cid in sorted(children.get(node["id"], []), key=lambda i: by_id[i]["y"]):
            emit(by_id[cid], level + 1)

    def depth_of(g: dict) -> int:
        d, cur, seen = 0, g, set()
        while cur.get("parent") and cur["parent"] not in seen:
            seen.add(cur["parent"])
            cur = next((x for x in groups if x["id"] == cur["parent"]), {})
            d += 1
        return d

    def inside_of(g: dict) -> list[dict]:
        """按显式 parent 归属，不靠几何包含——拖出框不该悄悄脱组。"""
        if any("parent" in n for n in plain):
            return [n for n in plain if n.get("parent") == g["id"]]
        return [n for n in plain
                if g["x"] <= n["x"] < g["x"] + g["w"] and g["y"] <= n["y"] < g["y"] + g["h"]]

    for g in groups:
        inside = inside_of(g)
        if not inside:
            if any(x.get("parent") == g["id"] for x in groups):
                lines += [f"{'#' * (2 + depth_of(g))} {g['label']}"]  # 纯容器组也要出现
            continue
        lines += [f"{'#' * (2 + depth_of(g))} {g['label']}"]
        ids = {n["id"] for n in inside}
        roots = [n for n in inside if parent.get(n["id"]) not in ids]
        for n in sorted(roots, key=lambda n: n["y"]):
            emit(n, 0)
        lines.append("")

    rest = [n for n in plain if n["id"] not in placed]
    if rest:
        lines += ["## 未分组"]
        for n in sorted(rest, key=lambda n: (n["x"], n["y"])):
            emit(n, 0)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
