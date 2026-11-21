"""Lane EV2: a Schedule 13D filed after 18 Dec 2024 carries an XML cover page; the sec_13d parser reads it.

Three real full-submission files from sec.gov are saved in fixtures/sec_13d/ (see its README).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dashboard.backend.events.errors import EdgarParseFatal
from dashboard.backend.events.parsers.sec_13d_cover import parse_13d_submission, parse_form_idx

FIX = Path(__file__).parent / "fixtures" / "sec_13d"
VIPER = "0001104659-26-105077"
AMEX = "0000004962-26-000356"
GENCO = "0001104659-26-081696"


def _text(accession: str) -> str:
    return (FIX / f"{accession}.txt").read_text()


# Header lines and divider copied from a real form.idx, then four rows.
_FORM_IDX = """Description:           Master Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    September 30, 2026
Comments:              webmaster@sec.gov
Anonymous FTP:         ftp://ftp.sec.gov/edgar/

Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
SC 13D/A    ALPHA HOLDINGS INC                                            1111111     2026-07-01  edgar/data/1111111/0001111111-26-000001-index.htm
SCHEDULE 13D  BETA CAPITAL LP                                             2222222     2026-07-02  edgar/data/2222222/0002222222-26-000002-index.htm
SCHEDULE 13D/A  GAMMA PARTNERS                                            3333333     2026-07-03  edgar/data/3333333/0003333333-26-000003-index.htm
SC 13G      DELTA FUND                                                    4444444     2026-07-04  edgar/data/4444444/0004444444-26-000004-index.htm
"""


def test_x1_form_idx_admits_the_new_forms():
    rows = list(parse_form_idx(_FORM_IDX))
    assert [r.form_type for r in rows] == ["SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A"]
    assert [r.accession_number for r in rows] == [
        "0001111111-26-000001", "0002222222-26-000002", "0003333333-26-000003"]


def test_x2_viper_xml_cover():
    r = parse_13d_submission(_text(VIPER), expected_accession_number=VIPER)
    assert r["payload"] == {"percent_owned": 42.9, "filer_name": "Diamondback Energy, Inc.",
                            "subject_name": "Viper Energy, Inc."}
    assert r["knowledge_at"] == 1788467449
    assert r["companies"] == [{"cik": 2074176, "role": "primary_issuer", "cusip": "64361Q101"}]
    assert r["natural_key"] == f"1539838_{VIPER}"
    raw = json.loads(r["raw_input"])
    assert raw["cover_format"] == "xml"
    assert raw["subject_cusip"] == "64361Q101"
    assert raw["percent_owned"] == 42.9
    assert raw["shares_owned"] == 145972177.0
    assert raw["reporting_persons"] == 3
    assert raw["event_date"] == "2026-09-01"
    assert raw["purpose"].startswith("Item 4 of the Schedule 13D is hereby amended")
    assert len(raw["purpose"]) <= 2000


def test_x3_prefixed_xml_cover():
    r = parse_13d_submission(_text(AMEX), expected_accession_number=AMEX)
    assert r["payload"] == {"percent_owned": 0.0, "filer_name": "AMERICAN EXPRESS CO",
                            "subject_name": "Global Business Travel Group, Inc."}
    assert r["companies"][0]["cusip"] == "37890B100"
    raw = json.loads(r["raw_input"])
    assert raw["cover_format"] == "xml"
    assert raw["shares_owned"] == 0.0
    assert raw["reporting_persons"] == 1
    assert raw["event_date"] == "2026-09-29"


def test_x4_old_format_unchanged_guard():
    r = parse_13d_submission(_text(GENCO), expected_accession_number=GENCO)
    assert r["payload"] == {"percent_owned": 14.4, "filer_name": "DIANA SHIPPING INC.",
                            "subject_name": "GENCO SHIPPING & TRADING LTD"}
    assert r["knowledge_at"] == 1783524143


def test_x4b_old_format_raw_input_keys():
    r = parse_13d_submission(_text(GENCO), expected_accession_number=GENCO)
    raw = json.loads(r["raw_input"])
    assert raw["cover_format"] == "text"
    for key in ("shares_owned", "reporting_persons", "event_date", "purpose"):
        assert key in raw and raw[key] is None
    assert list(raw)[:8] == ["accession_number", "filer_cik", "filer_name", "subject_cik",
                             "subject_name", "subject_cusip", "percent_owned",
                             "knowledge_at_epoch_utc"]


def test_x5_cut_short_xml_block_is_fatal():
    text = _text(VIPER)
    cut = text.index("<reportingPersonInfo>") + 40
    broken = text[:cut]
    with pytest.raises(EdgarParseFatal) as ei:
        parse_13d_submission(broken, expected_accession_number=VIPER)
    assert VIPER in str(ei.value)


def test_x5b_xml_block_with_no_closing_tag_is_fatal():
    text = _text(VIPER)
    broken = text.replace("</XML>", "", 1)
    with pytest.raises(EdgarParseFatal) as ei:
        parse_13d_submission(broken, expected_accession_number=VIPER)
    assert VIPER in str(ei.value)


def test_x6_quote_in_filer_name_keeps_raw_input_valid_json():
    text = _text(AMEX).replace("AMERICAN EXPRESS CO", 'AMERICAN "EXPRESS" CO', 1)
    r = parse_13d_submission(text, expected_accession_number=AMEX)
    raw = json.loads(r["raw_input"])
    assert raw["filer_name"] == 'AMERICAN "EXPRESS" CO'
