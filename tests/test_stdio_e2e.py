"""End to end: start the real server as a subprocess and talk MCP to it over stdio,
exactly as Claude Desktop or another MCP client would."""

import json
import sys

from mcp import Client, StdioServerParameters


async def test_server_speaks_mcp_over_stdio(sample_index, tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "evidence_mcp", "serve"],
        env={
            "EVIDENCE_MCP_INDEX_PATH": str(sample_index),
            "EVIDENCE_MCP_CACHE_DIR": str(tmp_path / "cache"),
            "EVIDENCE_MCP_MAX_CLASSIFICATION": "internal",
        },
    )
    async with Client(params) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("search_documents", {"query": "email retention"})

    assert "get_data" in tools and "search_documents" in tools
    top = json.loads(result.content[0].text)["results"][0]
    assert top["title"] == "Aurora Institute Records Retention Schedule"
