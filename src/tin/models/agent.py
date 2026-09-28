"""L3 研究员 Agent 层：软思路、每日简报、纠错日志、知识蒸馏、LLM 审计。

与 L1（facts）/ L2（judgment）的分工：
- L1 存事实，L2 存研究员写死的硬骨架（阈值、证伪条件），两者都不经过 LLM。
- L3 是「系统每天先替研究员想一遍」的那一层：读 L1+L2，产出简报草稿，
  研究员改/否的动作沉淀回 playbook。LLM 只在这一层出现。
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean, Date, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tin.db import Base, JSONType, UTCDateTime

# 简报生命周期。「草稿」= LLM 出完未经研究员过目，不得对外、不进快照。
BRIEF_STATUSES = ("草稿", "已采纳", "已修改", "已否决")
# 研究员对草稿的三种动作。每一次都写 brief_revisions，这是 agent 唯一的学习养料。
REVISION_ACTIONS = ("采纳", "改", "否")
# playbook 条目从哪来。「蒸馏」来的占比是 M2 的出口指标（≥ 1/3 说明闭环在转）。
PLAYBOOK_SOURCES = ("手写", "蒸馏", "历史日报", "判断抽取", "访谈")
PLAYBOOK_STATUSES = ("生效", "待确认", "停用")


class Researcher(Base):
    """研究员档案。一期只有一个人，建表是为了 M3 多人和将来的多租户留维度。"""

    __tablename__ = "researchers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 与 users.username 同名即同人；一期没有登录体系，不加外键避免强耦合
    username: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(32))
    varieties: Mapped[list] = mapped_column(JSONType, default=list)  # ["SN", ...]
    status: Mapped[str] = mapped_column(String(8), default="在职")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class PlaybookItem(Base):
    """L2 软思路的一个条目。

    刻意做成「一条条」而不是一大坨 prompt：一大坨无法审计——出了问题不知道是哪句话
    导致的。条目化之后每份简报能标注「本条结论用到了第 7、12、23 号思路」。
    """

    __tablename__ = "playbook_items"
    __table_args__ = (
        Index("ix_playbook_active", "researcher_id", "variety", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    researcher_id: Mapped[int] = mapped_column(ForeignKey("researchers.id"), index=True)
    variety: Mapped[str] = mapped_column(String(16), index=True)
    text: Mapped[str] = mapped_column(Text)
    # 何时适用。空 dict = 始终适用。形如 {"series_id": "MACRO.VIX", "when": "value > 19"}
    # 或 {"months": [9, 10]}；筛选逻辑在 brief 组装层，这里只存声明。
    scope: Mapped[dict] = mapped_column(JSONType, default=dict)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # 小的先进 prompt
    source: Mapped[str] = mapped_column(String(16), default="手写")
    # 溯源：蒸馏来的记 {"revision_ids": [...]}，历史日报抽的记 {"file": "..."}
    source_ref: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(8), default="待确认")
    version: Mapped[int] = mapped_column(Integer, default=1)
    # 使用率监测（SPEC §6.3 反向验收）：连续 30 天没被引用的条目提示停用，
    # 防止 playbook 只增不减、简报被噪音条目污染。
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class DailyBrief(Base):
    """每日简报草稿。行业标准三件套：基调 + 参考运行区间 + 操作倾向。

    区间是区间不是点位——点位预测在这行不是交付物，做了反而毁信任。
    """

    __tablename__ = "daily_briefs"
    __table_args__ = (
        UniqueConstraint("researcher_id", "variety", "trade_date", name="uq_brief_per_day"),
        Index("ix_brief_variety_date", "variety", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    researcher_id: Mapped[int] = mapped_column(ForeignKey("researchers.id"), index=True)
    variety: Mapped[str] = mapped_column(String(16))
    trade_date: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(8), default="草稿")

    tone: Mapped[str | None] = mapped_column(String(16))          # 取自 schemas.judgment.TONES
    # 「为什么是这个基调」。judgments 里有同名字段，研究员会预期简报也有；
    # 少了这一列，BriefDraft.tone_note 会在落库时被静默丢掉。
    tone_note: Mapped[str | None] = mapped_column(Text)
    range_low: Mapped[float | None] = mapped_column(Float)         # 参考运行区间下沿
    range_high: Mapped[float | None] = mapped_column(Float)
    range_series_id: Mapped[str | None] = mapped_column(String(80))  # 区间挂在哪条序列上
    stance: Mapped[str | None] = mapped_column(String(32))         # 逢低做多 / 高抛低吸 / 观望
    summary: Mapped[str | None] = mapped_column(Text)

    # 证据链。每条论断必带 series_id 或 playbook_item_id，由 llm 层的校验闸门保证；
    # 形如 [{"claim": "...", "series_id": "...", "value": 406000, "as_of": "...",
    #        "playbook_item_ids": [7, 12]}]
    claims: Mapped[list] = mapped_column(JSONType, default=list)
    # 本次实际喂进 prompt 的 playbook 条目，用于回答「为什么今天这么说」
    playbook_item_ids: Mapped[list] = mapped_column(JSONType, default=list)
    # 本次读到的 signals（阈值触发 / 证伪判定），确定性代码算好后才给 LLM 看
    signal_ids: Mapped[list] = mapped_column(JSONType, default=list)
    # 未能通过证据校验、已降级标灰的论断。非空即为质量警报。
    unverified: Mapped[list] = mapped_column(JSONType, default=list)

    model: Mapped[str | None] = mapped_column(String(64))
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reviewed_by: Mapped[str | None] = mapped_column(String(32))


class BriefRevision(Base):
    """纠错日志。研究员每一次「采纳 / 改 / 否」都落这张表。

    这是 agent 学习的唯一养料，不做公开命中率排名不代表可以省掉它：
    不统计人的准确率、不排名、不对外，但记录照常入库。
    """

    __tablename__ = "brief_revisions"
    __table_args__ = (Index("ix_revision_brief", "brief_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brief_id: Mapped[int] = mapped_column(ForeignKey("daily_briefs.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(8))
    field: Mapped[str | None] = mapped_column(String(32))  # tone/range/stance/claim:<idx>
    before: Mapped[dict] = mapped_column(JSONType, default=dict)
    after: Mapped[dict] = mapped_column(JSONType, default=dict)
    # 「为什么」。蒸馏的原料就是这个字段，空着等于这次修改白改。
    reason: Mapped[str | None] = mapped_column(Text)
    # 从简报展示到提交这一笔的秒数。秒级采纳 = 没看（SPEC §6.3 要监测的失败模式）。
    elapsed_seconds: Mapped[int | None] = mapped_column(Integer)
    distilled: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否已被蒸馏消费
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class PlaybookCandidate(Base):
    """从纠错日志蒸馏出的 playbook 候选条目，研究员确认后才升格为 PlaybookItem。

    不自动生效：LLM 可以独立出结论，但不能独立改写研究员的思路。
    """

    __tablename__ = "playbook_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    researcher_id: Mapped[int] = mapped_column(ForeignKey("researchers.id"), index=True)
    variety: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    scope: Mapped[dict] = mapped_column(JSONType, default=dict)
    source_revision_ids: Mapped[list] = mapped_column(JSONType, default=list)
    confidence: Mapped[int] = mapped_column(Integer, default=50)  # 0-100，LLM 自评
    status: Mapped[str] = mapped_column(String(8), default="待确认")
    # 确认后指向生成的条目，便于反查「这条思路是哪几次纠错换来的」
    promoted_item_id: Mapped[int | None] = mapped_column(ForeignKey("playbook_items.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class LlmCall(Base):
    """每一次 LLM 调用的全量留痕，一条不漏。三个理由，缺一不可：

    1. LLM 被允许独立出结论，「为什么今天这么说」必须答得上来——这是信任的前提；
    2. 将来训本地 30B，这张表就是现成的数据集；
    3. 换模型时能拿历史输入做回归对比，否则每次换模型都是盲飞。
    """

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_purpose_at", "purpose", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purpose: Mapped[str] = mapped_column(String(32))   # compose_brief / distill / extract_playbook / map_columns
    provider: Mapped[str] = mapped_column(String(16))  # anthropic / openai_compatible / fake
    model: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSONType)    # system / messages / tools，原样
    response: Mapped[dict | None] = mapped_column(JSONType)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    # 缓存命中是成本的主要变量：prefix 稳定时缓存读只要 0.1× 价格。
    # 这两列长期为 0 就说明 prompt 里混进了每次都变的东西（日期、UUID）。
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(8))     # ok / error / refusal
    error: Mapped[str | None] = mapped_column(Text)
    brief_id: Mapped[int | None] = mapped_column(ForeignKey("daily_briefs.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class VendorColumnMap(Base):
    """终端导出文件的列映射缓存：(文件指纹, 列名) → series_id。

    把「每次导出都要拿给 AI 认列」变成「只有新格式才要人看」。这是终端自动化里
    唯一真正的产品化点，其余都是搬运。

    指纹由表头结构算出（不含数据行），同一张报表换了日期仍命中同一条映射。
    """

    __tablename__ = "vendor_column_maps"
    __table_args__ = (
        UniqueConstraint("fingerprint", "column_name", name="uq_column_map"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor: Mapped[str] = mapped_column(String(32), index=True)  # SMM / Mysteel
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    column_name: Mapped[str] = mapped_column(String(128))
    # 外键保证映射只能指向已登记指标，与 judgment_series_refs 同一思路
    series_id: Mapped[str] = mapped_column(ForeignKey("indicators.series_id"))
    source: Mapped[str] = mapped_column(String(16), default="人工")  # 人工 / LLM建议
    confirmed_by: Mapped[str] = mapped_column(String(32))
    confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # 命中计数：长期为 0 的映射说明那张报表已经不导了，可以清理
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
