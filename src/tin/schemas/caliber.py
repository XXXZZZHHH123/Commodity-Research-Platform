from pydantic import BaseModel, ConfigDict

from tin.caliber.dictionary import (
    ContractKind,
    ImportCode,
    PriceType,
    SpotSource,
    StockScope,
    TCGrade,
    TimeType,
    WeightBasis,
)


class Caliber(BaseModel):
    """一个数字的度量前提。time_type 必填；其余维度按指标适用填写，取值受口径字典约束。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    time_type: TimeType
    price_type: PriceType | None = None
    contract_kind: ContractKind | None = None
    contract: str | None = None
    weight_basis: WeightBasis | None = None
    tc_grade: TCGrade | None = None
    stock_scope: StockScope | None = None
    spot_source: SpotSource | None = None
    import_code: ImportCode | None = None
    country: str | None = None
    note: str | None = None

    def dump(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True)

    def label(self) -> str:
        parts = [self.contract, self.price_type, self.stock_scope, self.weight_basis,
                 self.tc_grade, self.spot_source, self.import_code, self.country]
        return " · ".join(str(p) for p in parts if p)
