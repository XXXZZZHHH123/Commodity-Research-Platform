"""口径字典（04 附录 §3）。一期代码内唯一真源；指标登记与观测入库都以此校验。"""

from dataclasses import dataclass
from enum import StrEnum


class WeightBasis(StrEnum):
    实物吨 = "实物吨"
    金属吨 = "金属吨"


class PriceType(StrEnum):
    最新价 = "最新价"
    结算价 = "结算价"
    收盘价 = "收盘价"
    均价 = "均价"


class ContractKind(StrEnum):
    主力连续 = "主力连续"  # 数据商拼接的连续序列，换月跳价
    主力合约 = "主力合约"  # 本平台每日按持仓最大选出的具体合约，每点可追溯到合约代码
    具体合约 = "具体合约"


class TCGrade(StrEnum):
    度40 = "40度"
    度60 = "60度"


class StockScope(StrEnum):
    交易所库存 = "交易所库存"
    交易所注册仓单 = "交易所注册仓单"
    社会库存 = "社会库存"
    保税库存 = "保税库存"
    隐形库存 = "隐形库存"


class SpotSource(StrEnum):
    SMM = "SMM"
    长江有色 = "长江有色"


class ImportCode(StrEnum):
    精矿 = "精矿(HS2609)"
    精锡 = "精锡(HS8001)"
    废碎料 = "废碎料"


class TimeType(StrEnum):
    交易时点 = "交易时点"
    发布时点 = "发布时点"
    抓取时点 = "抓取时点"


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    enum: type[StrEnum]
    note: str


DIMENSIONS: tuple[Dimension, ...] = (
    Dimension("weight_basis", "重量口径", WeightBasis,
              "锡精矿进口两者相差 2–3 倍，必须标明；证伪条件“回万吨”按实物吨判定"),
    Dimension("price_type", "价格类型", PriceType,
              "跨期价差两端必须同类型；成交清淡合约默认用结算价"),
    Dimension("contract_kind", "合约标识", ContractKind,
              "主力连续与具体合约不可混算；主力连续在换月时会跳价"),
    Dimension("tc_grade", "TC 品位", TCGrade, "不同品位数值不可比"),
    Dimension("stock_scope", "库存范围", StockScope,
              "上期所每日只发注册仓单，交易所库存为周报；隐形库存无公开数据，只能来自调研"),
    Dimension("spot_source", "现货来源", SpotSource, "两家报价口径不同，页面可并列展示但不可混算"),
    Dimension("import_code", "进口口径", ImportCode, "不可合并计算"),
    Dimension("time_type", "时点类型", TimeType,
              "as_of 存数据所代表的时刻（交易所数据为收盘，统计数据为统计期末），不存抓取时点"),
)
