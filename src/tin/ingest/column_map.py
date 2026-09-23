"""终端导出文件的列映射学习缓存（SPEC §4.4 第 3 段）。

要解决的是一件很具体的事：数据商只给终端不给 API，研究员每次导出工作簿都得把它
拿给 AI 认一遍「哪一列是哪个指标」，认完才敢落库。**这里要把「每次都要认」变成
「只有新格式才要人看」**——第一次遇到某张报表时由人（将来也可以由 LLM 提建议、
人确认一次）把列名钉到 `series_id` 上，之后同指纹的导出自动命中。

与 `vendor_terminal.py` 的分工，别搞混：

- `vendor_terminal` 走的是**供应商编码**通道，编码稳定、唯一、与显示名无关，
  能按编码对上的一律走编码，这里不掺和；
- 这份缓存只接编码通道接不住的那部分：编码在库里没有任何绑定、退而求其次只能
  按**列名**认的列。所以它是兜底通道，不是替代通道。

映射落在 `vendor_column_maps`，`series_id` 带外键——**只能指向已登记指标**，
与 `judgment_series_refs`「证伪条件只能引用已登记指标」是同一招。缓存里不可能
出现一条指向空气的映射，这一层由数据库兜底，不靠调用方自觉。
"""

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

# 指纹算法版本前缀。将来若改算法（比如决定把列顺序踢出指纹），换版本号即可，
# 新旧指纹天然不相等，老映射不会被新算法悄悄命中一条含义已变的记录。
FP_VERSION = "v1"
FP_LEN = 32  # sha256 取前 32 位十六进制；连同前缀 35 字符，塞得进 String(64)

# 映射的来源。与 `VendorColumnMap.source` 的取值域一致：
# 「人工」= 人在确认页点过；「LLM建议」= LLM 提的、走了自动采纳通道。
SOURCE_HUMAN = "人工"
SOURCE_LLM = "LLM建议"
SOURCES = (SOURCE_HUMAN, SOURCE_LLM)

# 建议的来源（注意这是**建议**的来源，不是**映射**的来源）。
# 现阶段只有确定性规则匹配；LLM 接入层就绪后新增一条 SUGGEST_LLM 的通道，
# 返回结构不变，调用方与前端不用改。
SUGGEST_RULE = "规则匹配"
SUGGEST_LLM = "LLM建议"

COLUMN_NAME_MAX = 128  # 对齐 `VendorColumnMap.column_name` 的列宽

# 名称归一化：去掉空白与各式分隔符、全角转半角、大小写拉平。
# 与 `excel_importer._norm` 同一套规则，两条通道对「同一个名字」的认定必须一致。
_JUNK = re.compile(r"[\s（）()·\-_/：:，,]+")


def _norm(text) -> str:
    return _JUNK.sub("", unicodedata.normalize("NFKC", str(text))).lower()


def _key(column_name) -> str:
    """列名入库前的规范化：归一化只用于比对，存的仍是**可读的原名**（截断到列宽）。

    存原名是为了让人在确认页和 `hit_count` 清理时看得懂这条映射说的是哪一列；
    比对用归一化，是为了「锡锭: 库存: 中国（周）」和「锡锭：库存：中国(周)」算同一列。
    """
    return str(column_name).strip()[:COLUMN_NAME_MAX]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- 指纹 ----------

def fingerprint(headers: Sequence[str]) -> str:
    """由**表头结构**算指纹，不含任何数据行。

    两条取舍，都是为了「指纹变化 = 报表改版」这一条语义：

    1. **只吃表头、不吃数据行。** 同一张报表今天导 9/22、明天导 9/23，数据行全变、
       行数也变，指纹必须纹丝不动——否则缓存每天失效一次，等于没做。
    2. **列顺序参与指纹（换了顺序就是另一个指纹）。** 这条是刻意选的，理由有三：
       - 映射按**列名**存，顺序本身不影响命中，所以「顺序敏感」换来的不是正确性
         而是**保守**：顺序变了，最可能的解释是这张报表被改版了（加列、删列、
         换了导出模板），而改版之后同名列的口径未必还是原来那个口径。宁可让人
         再确认一次，也不要让一条口径已变的列悄悄套用旧映射写进事实层；
       - 一期已经吃过「无声裂成两个序列」的亏（见 `resolve_ids` 的注释）。**数据
         层面的错必须吵，不能安静**，多一次人工确认的成本远低于污染事实层；
       - 代价可控：真的只是拖动了列顺序，人多确认一次，新指纹入库，此后照常命中。
         错误方向是"多问一次"，不是"默默写错"。

    空列名不参与指纹：终端导出常在右侧拖出几列空白，它们既不承载映射也不稳定。
    """
    parts = [f"{i}\x1f{_norm(h)}" for i, h in enumerate(
        [h for h in headers if h is not None and str(h).strip()])]
    digest = hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()
    return f"{FP_VERSION}:{digest[:FP_LEN]}"


# ---------- 查缓存 ----------

def lookup(session: Session, fp: str, headers: Sequence[str],
           *, count_hit: bool = True) -> tuple[dict[str, str], list[str]]:
    """查这张报表的列映射缓存。

    返回 `(已命中的 列名 → series_id, 未命中的列名)`。**未命中的那一串就是要交给
    人看的东西**——调用方把它上报出去，而不是自己猜一个。

    `count_hit=False` 用于试算与确认页预览：那些路径只是看看，不该计入命中数。
    `hit_count` 的用途是回答「这张报表还在导吗」，长期为 0 的映射可以清理掉，
    所以它记的应当是真正入库的次数。
    """
    wanted = [h for h in headers if h is not None and str(h).strip()]
    if not wanted:
        return {}, []
    from tin.models import VendorColumnMap

    rows = session.scalars(
        select(VendorColumnMap).where(VendorColumnMap.fingerprint == fp)).all()
    by_norm = {_norm(r.column_name): r for r in rows}

    mapped: dict[str, str] = {}
    unmapped: list[str] = []
    now = _now()
    for header in wanted:
        row = by_norm.get(_norm(header))
        if row is None:
            unmapped.append(str(header))
            continue
        mapped[str(header)] = row.series_id
        if count_hit:
            row.hit_count = (row.hit_count or 0) + 1
            row.last_hit_at = now
    if count_hit and mapped:
        session.flush()
    return mapped, unmapped


# ---------- 人工确认 ----------

def confirm(session: Session, vendor: str, fp: str, mapping: dict[str, str],
            actor: str, source: str = SOURCE_HUMAN) -> int:
    """把人确认过的列映射写进缓存，返回写入（含更新）的条数。

    幂等：同一 `(fingerprint, column_name)` 重复确认就地更新，不报错、不写重——
    人在确认页改主意重点一次是常态，不该要求他先去删旧记录。

    这里**不替调用方校验 `series_id` 存不存在**：外键会拦，而且拦得住。
    自己再查一遍只是把同一条规则写两遍，两份规则早晚不一致。
    """
    if source not in SOURCES:
        raise ValueError(f"映射来源只能是 {'、'.join(SOURCES)}，收到「{source}」")
    from tin.models import VendorColumnMap

    now = _now()
    written = 0
    # 按归一化名索引既有记录：唯一约束管的是字面量，而「锡锭：库存」与「锡锭: 库存」
    # 在字面量上是两行。按归一化认人，才不会给同一列攒出两条打架的映射。
    existing = {_norm(r.column_name): r for r in session.scalars(
        select(VendorColumnMap).where(VendorColumnMap.fingerprint == fp))}
    for raw_name, series_id in mapping.items():
        name = _key(raw_name)
        if not name or not series_id:
            continue
        row = existing.get(_norm(name))
        if row is None:
            row = VendorColumnMap(
                vendor=vendor, fingerprint=fp, column_name=name, series_id=series_id,
                source=source, confirmed_by=actor, confirmed_at=now, hit_count=0)
            session.add(row)
            existing[_norm(name)] = row
        else:
            row.vendor, row.series_id = vendor, series_id
            row.source, row.confirmed_by, row.confirmed_at = source, actor, now
        written += 1
    # 立刻 flush：外键要在 confirm() 这一刻炸，而不是拖到调用方几百行之后的 commit，
    # 那时候报出来的堆栈已经指不回是哪一列绑错了。
    session.flush()
    return written


# ---------- 候选建议 ----------

@dataclass(frozen=True)
class _Candidate:
    series_id: str
    name: str
    confidence: int
    reason: str


# 置信度分档。只有「编码命中」和「名称完全一致」敢给高分，其余一律压到需要人看的区间——
# 建议是给人省事的，不是替人拍板的。
_CONF_CODE = 99
_CONF_EXACT = 95
_CONF_CONTAIN = 70
_FUZZY_FLOOR = 0.55
_TOP_N = 3


def suggest(session: Session, vendor: str, headers: Sequence[str],
            unmapped: Sequence[str]) -> list[dict]:
    """给未命中的列出候选 `series_id`，**现阶段只做库内的确定性匹配，不调 LLM**。

    匹配依据全部来自库内已登记指标的 `name` 与 `vendor_code`，可复现、可解释、
    不花钱、不联网。四档：编码完全一致 > 名称完全一致 > 互为子串 > 模糊相似。

    返回形如：

        [{"column_name": "锡锭：库存：中国（周）",
          "source": "规则匹配",
          "candidates": [{"series_id": "MYSTEEL.SN.stock.social",
                          "name": "锡锭社会库存", "confidence": 70,
                          "reason": "列名与指标名互为子串"}]}]

    **给 LLM 预留的接口就是这个返回结构本身**：`source` 标明这批建议是谁提的，
    `confidence` 是它的自评。LLM 接入层就绪后，在 `src/tin/llm/` 里实现一个
    `suggest_by_llm(...) -> list[dict]`，产出同样形状（`source` 取 `SUGGEST_LLM`）
    的结果即可；调用方与前端不用改，两批建议可以直接合并排序。无论建议来自规则
    还是 LLM，落库都必须过 `confirm()`——**LLM 可以提建议，不能自己往事实层里钉列**。

    `headers` 是这张报表的完整列名（当前用不上，保留给 LLM 通道：判断某一列是什么，
    上下文里有同表其它列会准得多）。
    """
    from tin.models import Indicator

    targets = [h for h in unmapped if h is not None and str(h).strip()]
    if not targets:
        return []

    pool = [i for i in session.scalars(select(Indicator)) if i.status == "可用"]
    out = []
    for header in targets:
        target = _norm(header)
        scored: list[_Candidate] = []
        for ind in pool:
            name = _norm(ind.name)
            code = _norm(ind.vendor_code) if ind.vendor_code else ""
            # 子串判定都设了长度下限：两三个字符的串随便都能套进一个长列名里，
            # 那不叫匹配，叫噪音。
            if code and (code == target or (len(code) >= 5 and code in target)):
                scored.append(_Candidate(ind.series_id, ind.name, _CONF_CODE, "列名里带着供应商编码"))
            elif name and name == target:
                scored.append(_Candidate(ind.series_id, ind.name, _CONF_EXACT, "指标名与列名完全一致"))
            elif len(name) >= 3 and len(target) >= 3 and (name in target or target in name):
                scored.append(_Candidate(ind.series_id, ind.name, _CONF_CONTAIN, "列名与指标名互为子串"))
            else:
                ratio = SequenceMatcher(None, target, name).ratio()
                if ratio >= _FUZZY_FLOOR:
                    # 压到子串档之下：模糊相似最容易把 40%TC 和 60%TC 这种
                    # "只差一个字、数值完全不可比" 的变体排到前面去。
                    scored.append(_Candidate(ind.series_id, ind.name, int(ratio * 60),
                                             f"名称相似度 {ratio:.0%}"))
        scored.sort(key=lambda c: (-c.confidence, c.series_id))
        out.append({
            "column_name": str(header),
            "vendor": vendor,
            "source": SUGGEST_RULE,
            "candidates": [
                {"series_id": c.series_id, "name": c.name,
                 "confidence": c.confidence, "reason": c.reason}
                for c in scored[:_TOP_N]
            ],
        })
    return out
