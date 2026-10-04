"""The Copilot Studio connector matches what the server expects from Entra ID tokens."""

from pathlib import Path

import yaml

from evidence_mcp.auth import EntraSettings

FOLDER = Path(__file__).resolve().parents[1] / "integrations" / "copilot-studio"


def test_connector_is_one_streamable_mcp_operation():
    spec = yaml.safe_load((FOLDER / "connector.swagger.yaml").read_text(encoding="utf-8"))
    assert spec["swagger"] == "2.0" and spec["schemes"] == ["https"]
    assert list(spec["paths"]) == ["/mcp"]
    operation = spec["paths"]["/mcp"]["post"]
    assert operation["x-ms-agentic-protocol"] == "mcp-streamable-1.0"


def test_connector_asks_for_the_scope_the_server_requires():
    spec = yaml.safe_load((FOLDER / "connector.swagger.yaml").read_text(encoding="utf-8"))
    oauth = spec["securityDefinitions"]["entra"]
    settings = EntraSettings(tenant_id="t", audience="api://policy-evidence")
    scope = f"{settings.audience}/{settings.required_scope}"
    assert scope in oauth["scopes"]
    assert oauth["tokenUrl"].endswith("/oauth2/v2.0/token")  # v2 tokens, the issuer we check


def test_instructions_name_only_tools_the_server_has(server):
    import asyncio
    import re

    from mcp import Client

    async def names():
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    text = (FOLDER / "agent-instructions.md").read_text(encoding="utf-8")
    mentioned = set(re.findall(r"\b(search_\w+|describe_\w+|get_\w+|list_\w+)\b", text))
    assert mentioned and mentioned <= asyncio.run(names())
