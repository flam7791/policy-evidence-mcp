from evidence_mcp.sdmx import SdmxClient, parse_csv, parse_dataflows, parse_structure

from .conftest import FIXTURES


def test_parse_dataflows_reads_ids_versions_and_english_names():
    flows = parse_dataflows((FIXTURES / "dataflows.xml").read_bytes())
    assert len(flows) == 4
    msti = flows[0]
    assert (msti.agency, msti.id, msti.version) == ("OECD.STI.STP", "DSD_MSTI@DF_MSTI", "1.3")
    assert msti.name.startswith("Main Science and Technology Indicators")


def test_parse_structure_dimensions_in_key_order_with_labels():
    s = parse_structure((FIXTURES / "structure_msti.xml").read_bytes(), "DSD_MSTI@DF_MSTI")
    assert s.key_template == "REF_AREA.FREQ.MEASURE.UNIT_MEASURE.PRICE_BASE.TRANSFORMATION"
    assert s.time_dimension == "TIME_PERIOD"
    area = s.dimensions[0]
    assert area.name == "Reference area"
    assert area.codelist == "CL_AREA"
    assert area.codes["DEU"] == "Germany"  # English label chosen even when French comes first


def test_attribute_relationships_are_not_mistaken_for_dimensions():
    # The fixture's AttributeList contains <Dimension><Ref .../></Dimension> elements.
    s = parse_structure((FIXTURES / "structure_msti.xml").read_bytes(), "DSD_MSTI@DF_MSTI")
    assert len(s.dimensions) == 6


def test_parse_csv_lifts_constant_columns_and_drops_empty_ones():
    table = parse_csv((FIXTURES / "data_msti.csv").read_text(encoding="utf-8"), max_rows=100)
    assert table.total_rows == 4
    assert "Time period" not in table.columns  # empty label column dropped
    assert table.constant_columns["MEASURE"] == "G"
    assert table.constant_columns["Unit of measure"] == "Percentage of GDP"
    assert {"REF_AREA", "TIME_PERIOD", "OBS_VALUE"} <= set(table.columns)


def test_parse_csv_truncates_but_reports_total():
    table = parse_csv((FIXTURES / "data_msti.csv").read_text(encoding="utf-8"), max_rows=1)
    assert len(table.rows) == 1 and table.total_rows == 4 and table.truncated


def test_parse_csv_empty():
    assert parse_csv("", 10).total_rows == 0


def test_search_ranks_name_matches_first(fetcher):
    client = SdmxClient(fetcher)
    results = client.search("R&D expenditure", limit=5)
    assert results[0].id == "DSD_MSTI@DF_MSTI"
    assert all(r.id != "DSD_LFS@DF_IALFS_UNE_M" for r in results)


def test_latest_version_resolves_to_highest(fetcher):
    client = SdmxClient(fetcher)
    assert client.resolve_version("OECD.STI.STP", "DSD_MSTI@DF_MSTI", "latest") == "1.3"
    assert client.resolve_version("OECD.STI.STP", "DSD_MSTI@DF_MSTI", "1.2") == "1.2"


def test_data_404_means_empty_result_not_error(fetcher):
    client = SdmxClient(fetcher)
    table, url, _ = client.data(
        "OECD.STI.STP", "DSD_MSTI@DF_MSTI", "1.3", "ITA.A.G...", None, None, 10
    )
    assert table.total_rows == 0
    assert "ITA.A.G..." in url
