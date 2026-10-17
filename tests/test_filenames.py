"""Filename parsing: every real naming variant parses; junk does not."""

from __future__ import annotations

from livecell_audit.filenames import parse_filename


def test_all_eight_cell_lines_parse():
    names = [
        "A172_Phase_A7_1_01d04h00m_3.tif",
        "BT474_Phase_B3_1_03d00h00m_3.tif",
        "BV2_Phase_C4_1_01d16h00m_3.tif",
        "Huh7_Phase_A10_2_00d00h00m_3.tif",
        "MCF7_Phase_B3_1_00d12h00m_1.tif",
        "SHSY5Y_Phase_D10_1_01d16h00m_4.tif",
        "SkBr3_Phase_H3_2_00d00h00m_1.tif",
        "SKOV3_Phase_G4_1_00d20h00m_3.tif",
    ]
    for name in names:
        parsed = parse_filename(name)
        assert parsed is not None, name
        assert parsed["extension"] == "tif"


def test_fields_split_correctly():
    parsed = parse_filename("Huh7_Phase_A10_2_00d00h00m_3.tif")
    assert parsed == {
        "cell_line": "Huh7",
        "well": "A10",
        "well_row": "A",
        "well_col": 10,
        "field": 2,
        "timestamp": "00d00h00m",
        "days": 0,
        "hours": 0,
        "minutes": 0,
        "time_hours": 0.0,
        "crop": 3,
        "extension": "tif",
    }
    parsed = parse_filename("A172_Phase_A7_1_01d04h00m_3.tif")
    assert parsed["cell_line"] == "A172"
    assert parsed["well"] == "A7"
    assert parsed["well_row"] == "A"
    assert parsed["well_col"] == 7
    assert parsed["field"] == 1
    assert parsed["timestamp"] == "01d04h00m"
    assert parsed["time_hours"] == 28.0


def test_alternate_extensions_and_multidigit_wells():
    assert parse_filename("A172_Phase_H12_3_10d00h30m_4.tiff")["well"] == "H12"
    assert parse_filename("A172_Phase_H12_3_10d00h30m_4.tiff")["time_hours"] == 240.5
    assert parse_filename("A172_Phase_A7_1_00d00h00m_1.png")["extension"] == "png"


def test_invalid_names_return_none():
    for bad in [
        "",
        "BADNAME.tif",
        "A172_Phase_A7_1.tif",
        "A172_Phase_Z7_1_00d00h00m_1.tif",  # well row outside A-H
        "A172_Phase_A7_1_00d00h00m_1.jpg",
        "A172_Phase_A0_1_00d00h00m_1.tif",  # well column 0
        None,
        123,
    ]:
        assert parse_filename(bad) is None, bad
