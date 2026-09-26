"""Live smoke test against the real OECD API (3 requests; results are cached afterwards).

The unit tests use recorded fixtures and never touch the network. Run this script by hand to
check that the live service still answers in the format the parsers expect:

    python scripts/smoke_live.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import Client

from evidence_mcp.config import Settings
from evidence_mcp.server import create_server


async def main() -> int:
    server = create_server(Settings.from_env())
    async with Client(server) as client:

        async def call(tool: str, args: dict) -> dict | None:
            result = await client.call_tool(tool, args)
            text = result.content[0].text
            if result.is_error:
                print(f"FAIL {tool}: {text}")
                return None
            print(f"ok   {tool}")
            return json.loads(text)

        found = await call("search_datasets", {"query": "research and development expenditure"})
        if not found or not found["results"]:
            return 1
        flow = next(
            (r for r in found["results"] if r["dataflow_id"] == "DSD_MSTI@DF_MSTI"),
            found["results"][0],
        )
        flow_ref = f"{flow['agency']}:{flow['dataflow_id']} v{flow['version']}"
        print(f"     using {flow_ref} ({flow['name']})")

        described = await call(
            "describe_dataset",
            {
                "agency": flow["agency"],
                "dataflow_id": flow["dataflow_id"],
                "version": flow["version"],
            },
        )
        if not described:
            return 1
        print(f"     key template: {described['key_template']}")
        for dim in described["dimensions"]:
            sample = ", ".join(list(dim["codes"])[:5])
            print(f"     {dim['position']}. {dim['id']} ({dim['code_count']} codes): {sample}")

        slots = len(described["dimensions"])
        area = "FRA" if "REF_AREA" in described["key_template"].split(".")[:1] else ""
        key = area + "." * (slots - 1)
        data = await call(
            "get_data",
            {
                "agency": flow["agency"],
                "dataflow_id": flow["dataflow_id"],
                "version": described["version"],
                "key": key,
                "start_period": "2020",
                "max_rows": 5,
            },
        )
        if not data:
            return 1
        print(f"     {data['total_rows']} rows for key '{key}'; columns: {data['columns']}")
        print(f"     constant columns: {list(data['constant_columns'])}")
        print(f"     first row: {data['rows'][0] if data['rows'] else '(none)'}")
        print(f"     cite as: {data['citation']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
