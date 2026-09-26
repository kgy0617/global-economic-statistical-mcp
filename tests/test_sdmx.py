import pytest
from conftest import item_row

from global_economic_statistical_mcp import ecos_sdmx as sdmx


@pytest.mark.parametrize("code", ["0101000", "*AA", "A$B", "코드", "a.b/c"])
def test_sdmx_id_is_valid_and_reversible(code):
    identifier = sdmx.sdmx_id(code)
    assert all(ch.isascii() and (ch.isalnum() or ch in "_@$-") for ch in identifier)
    assert sdmx.ecos_code(identifier) == code


@pytest.mark.parametrize(
    ("cycle", "value", "expected"),
    [
        ("A", "2024", "2024"),
        ("S", "2024S2", "2024-S2"),
        ("Q", "2024Q1", "2024-Q1"),
        ("M", "202401", "2024-01"),
        ("D", "20240115", "2024-01-15"),
    ],
)
def test_sdmx_time(cycle, value, expected):
    assert sdmx.sdmx_time(cycle, value) == expected


def test_urns_follow_sdmx_syntax():
    assert sdmx.dataflow_urn("901Y009") == (
        "urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=ECOS_MCP:901Y009(1.0)"
    )
    assert sdmx.concept_urn("901Y009", "FREQ") == (
        "urn:sdmx:org.sdmx.infomodel.conceptscheme.Concept=ECOS_MCP:CS_901Y009(1.0).FREQ"
    )


def test_build_structure_merges_cycles_and_groups():
    rows = [
        item_row("T1", 1, "A", "상위", cycle="M"),
        item_row("T1", 1, "A", "상위", cycle="Q", start="2000Q1", end="2026Q2"),
        item_row("T1", 1, "A1", "하위", parent="A"),
        item_row("T1", 2, "X", "측정"),
    ]
    structure = sdmx.build_table_structure("T1", "테스트", rows)
    assert structure.cycles == ["M", "Q"]
    assert structure.series_key == ["FREQ", "ITEM_CODE1", "ITEM_CODE2"]
    first = structure.dimensions[0].codes[0]
    assert first.availability == {"M": "200001~202608", "Q": "2000Q1~2026Q2"}
    assert structure.dimensions[0].codes[1].parent == "A"


def test_structure_message_keeps_known_parents_only():
    rows = [item_row("T1", 1, "A", "상위"), item_row("T1", 1, "A1", "하위", parent="A")]
    structure = sdmx.filter_structure(sdmx.build_table_structure("T1", "테스트", rows), "하위")
    message = sdmx.structure_message(structure)
    codelist = next(c for c in message["data"]["codelists"] if c["id"] == "CL_T1_ITEM_CODE1")
    assert codelist["isPartial"] is True
    assert codelist["codes"] == [
        {"id": "A1", "name": "하위", "annotations": [
            {"type": "ECOS_UNIT", "title": "원"},
            {"type": "ECOS_AVAILABILITY", "title": "M:200001~202608"},
        ]}
    ]


def test_data_message_series_keys_and_attributes():
    rows = [
        {"STAT_NAME": "환율", "ITEM_CODE1": "0000001", "ITEM_NAME1": "USD", "ITEM_CODE2": "0000100",
         "ITEM_NAME2": "평균", "UNIT_NAME": "원", "TIME": "202401", "DATA_VALUE": "1300"},
        {"STAT_NAME": "환율", "ITEM_CODE1": "0000001", "ITEM_NAME1": "USD", "ITEM_CODE2": "0000200",
         "ITEM_NAME2": "말일", "UNIT_NAME": "원", "TIME": "202401", "DATA_VALUE": "1310"},
        {"STAT_NAME": "환율", "ITEM_CODE1": "0000001", "ITEM_NAME1": "USD", "ITEM_CODE2": "0000100",
         "ITEM_NAME2": "평균", "UNIT_NAME": "원", "TIME": "202402", "DATA_VALUE": "1320"},
    ]
    message = sdmx.data_message(rows, stat_code="731Y004", cycle="M", notes=["참고", None])
    dataset = message["data"]["dataSets"][0]
    assert dataset["series"] == {
        "0:0:0": {"attributes": [0], "observations": {"0": [1300], "1": [1320]}},
        "0:0:1": {"attributes": [0], "observations": {"0": [1310]}},
    }
    structure = message["data"]["structures"][0]
    assert structure["attributes"]["series"][0]["values"] == [{"value": "원"}]
    assert [a["type"] for a in structure["annotations"]] == ["ECOS_MCP_MAPPING", "NOTE"]
    assert dataset["annotations"] == [0, 1]
