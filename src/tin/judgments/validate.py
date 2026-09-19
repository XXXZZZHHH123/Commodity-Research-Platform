"""判断“能否生效”的硬规则（FR-1.1 / 1.3 / 1.4）。任何一条不满足都不允许提交为生效。"""

from tin.schemas.judgment import JudgmentIn


def activation_blockers(j: JudgmentIn) -> list[str]:
    out: list[str] = []
    c = j.contradiction
    for side, label in ((c.bull, "多方"), (c.bear, "空方")):
        if not side.claim.strip() or not side.evidence:
            out.append(f"主逻辑{label}缺失：必须填写多空两边，每边至少一条证据")
    if not j.marginal_focus:
        out.append("短线边际至少一条")
    elif any(m.series_id is None for m in j.marginal_focus):
        out.append("短线边际的每一条都必须关联一个已登记指标")
    if not (j.pricing_power.short and j.pricing_power.long):
        out.append("边际定价权必须分别填写短期与中长期")
    if not 3 <= len(j.whitelist) <= 8:
        out.append(f"指标白名单须为 3–8 个，当前 {len(j.whitelist)} 个")
    if not j.tone:
        out.append("基调必填")
    if len(j.thresholds) >= 2:
        ids = [t.id for t in j.thresholds]
        if j.priority_rule is None:
            out.append(f"存在 {len(ids)} 条阈值，必须指定冲突优先级（同时触发且指向相反动作时先听哪条）")
        elif set(j.priority_rule.order) != set(ids):
            out.append(f"冲突优先级必须覆盖全部阈值：缺 {sorted(set(ids) - set(j.priority_rule.order))}")
    return out
