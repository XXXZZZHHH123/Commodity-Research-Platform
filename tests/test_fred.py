from datetime import date, datetime

from tin.config import NEW_YORK
from tin.export.board import contract_curve, macro_cards
from tin.ingest.fred import SERIES_BY_ID, parse_series
from tin.ingest.runner import store


D = date(2026, 9, 18)


def test_fred_parser_skips_missing_and_keeps_source_metadata(session):
    spec = SERIES_BY_ID["FRED.DGS10"]
    csv = "observation_date,DGS10\n2026-09-16,4.90\n2026-09-17,.\n2026-09-18,4.94\n"
    batch = parse_series(csv, spec, through=D, source_url="https://fred.test/DGS10")
    assert [o.value for o in batch.observations] == [4.90, 4.94]
    assert batch.indicators[0].series_id == "FRED.DGS10"
    assert batch.observations[-1].note == "FRED ID DGS10；原始发布方 U.S. Treasury；来源层级 L1"


def test_market_series_uses_new_york_close(session):
    spec = SERIES_BY_ID["FRED.SP500"]
    batch = parse_series("observation_date,SP500\n2026-09-17,7637.76\n", spec, through=D)
    assert batch.observations[0].as_of == datetime(2026, 9, 17, 16, tzinfo=NEW_YORK)
    assert batch.observations[0].caliber.price_type == "收盘价"


def test_macro_cards_build_change_and_sparkline(session):
    spec = SERIES_BY_ID["FRED.DGS10"]
    batch = parse_series("observation_date,DGS10\n2026-09-16,4.90\n2026-09-18,4.94\n", spec, through=D)
    store(session, batch)
    card = macro_cards(session, D, (spec.series_id,))[0]
    assert card["status"] == "ok"
    assert card["value"] == "4.94"
    assert card["change"] == "+4 bp"
    assert len(card["points"].split()) == 2


def test_term_structure_uses_all_contracts_and_marks_main(loaded):
    curve = contract_curve(loaded, "SN", D)
    assert len(curve["rows"]) == 12
    assert curve["rows"][0]["contract"] == "SN2610"
    assert curve["rows"][0]["main"] is True
    assert curve["points"]
