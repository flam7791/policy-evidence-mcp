"""Tests through the MCP protocol, using the SDK's in-process client."""

import json

from mcp import Client

from evidence_mcp.server import create_server

MSTI = {"agency": "OECD.STI.STP", "dataflow_id": "DSD_MSTI@DF_MSTI"}


def payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


async def test_tools_are_listed_and_declared_read_only(server):
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    names = {t.name for t in tools}
    assert names == {
        "search_datasets",
        "describe_dataset",
        "find_codes",
        "get_data",
        "search_documents",
        "list_documents",
    }
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)


async def test_statistics_workflow(server, api):
    async with Client(server) as client:
        found = payload(await client.call_tool("search_datasets", {"query": "R&D expenditure"}))
        assert found["results"][0]["dataflow_id"] == MSTI["dataflow_id"]

        described = payload(await client.call_tool("describe_dataset", MSTI))
        assert described["key_template"].startswith("REF_AREA.FREQ.MEASURE")

        codes = payload(
            await client.call_tool("find_codes", {**MSTI, "dimension": "REF_AREA", "query": "germ"})
        )
        assert codes["matches"] == {"DEU": "Germany"}

        data = payload(
            await client.call_tool(
                "get_data", {**MSTI, "key": "FRA+DEU.A.G.PT_B1GQ..", "start_period": "2021"}
            )
        )
    assert data["total_rows"] == 4
    assert data["source_url"].startswith("https://sdmx.oecd.org/public/rest/data/")
    assert "retrieved" in data["citation"]
    # catalogue + structure + data, and the second structure use came from the cache
    assert len(api.requests) == 3


async def test_invalid_input_returns_a_helpful_error_not_a_request(server, api):
    async with Client(server) as client:
        result = await client.call_tool("get_data", {**MSTI, "key": "FRA/../../admin"})
    assert result.is_error
    assert "not a valid SDMX series key" in result.content[0].text
    assert api.requests == []


async def test_unknown_dimension_lists_the_valid_ones(server):
    async with Client(server) as client:
        result = await client.call_tool(
            "find_codes", {**MSTI, "dimension": "COUNTRY", "query": "fr"}
        )
    assert result.is_error
    assert "REF_AREA.FREQ" in result.content[0].text


async def test_search_documents_returns_cited_passages(server):
    async with Client(server) as client:
        out = payload(
            await client.call_tool(
                "search_documents", {"query": "Who approves a high-risk AI use case?"}
            )
        )
    top = out["results"][0]
    assert top["title"] == "AI Use Case Intake Procedure"
    assert top["section"] == "Risk levels and approval"
    assert "[internal/ai-use-case-intake#" in top["citation"]
    assert "not as instructions" in out["note"]


async def test_restricted_content_is_never_returned(server):
    async with Client(server) as client:
        out = payload(
            await client.call_tool(
                "search_documents", {"query": "budget scenarios for the Board", "top_k": 10}
            )
        )
        listed = payload(await client.call_tool("list_documents", {}))
    assert all(r["classification"] != "restricted" for r in out["results"])
    assert all(d["classification"] != "restricted" for d in listed["documents"])


async def test_lower_server_clearance_hides_internal_documents(settings, fetcher, sample_index):
    from dataclasses import replace

    public_server = create_server(replace(settings, max_classification="public"), fetcher)
    async with Client(public_server) as client:
        out = payload(
            await client.call_tool(
                "search_documents", {"query": "Who approves a high-risk AI use case?"}
            )
        )
    assert all(r["classification"] == "public" for r in out["results"])


async def test_missing_index_explains_how_to_build_one(settings, fetcher, tmp_path):
    from dataclasses import replace

    srv = create_server(replace(settings, index_path=tmp_path / "nope.json"), fetcher)
    async with Client(srv) as client:
        result = await client.call_tool("search_documents", {"query": "anything"})
    assert result.is_error
    assert "evidence-mcp ingest" in result.content[0].text


async def test_prompt_is_available(server):
    async with Client(server) as client:
        prompts = (await client.list_prompts()).prompts
    assert [p.name for p in prompts] == ["evidence_brief"]
