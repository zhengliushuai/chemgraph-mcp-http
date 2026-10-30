"""End-to-end: produce a fresh artifact over MCP, then read it back over MCP."""

import asyncio
import base64
import hashlib
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = os.environ.get("MCP_BASE", "http://127.0.0.1:9003")
ROOT = os.environ.get("CHEMGRAPH_DIR") or os.getcwd()
NAME = "verify_ethanol.xyz"


async def main():
    async with streamablehttp_client(f"{BASE}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()

            made = await s.call_tool("smiles_to_coordinate_file",
                                     {"smiles": "CCO", "output_file": NAME})
            print("produced:", (made.content[0].text or "")[:160].replace("\n", " "),
                  "| isError:", made.isError)

            got = await s.call_tool("read_artifact", {"path": NAME})
            p = json.loads(got.content[0].text)
            data = base64.b64decode(p["content_base64"]) if p.get("content_base64") else b""

            disk = os.path.join(ROOT, NAME)
            on_disk = open(disk, "rb").read()
            print(f"read_artifact: {p['filename']} {p['size_bytes']} B "
                  f"mime={p['mime']} note={p['note']}")
            print(f"  b64 roundtrip == disk bytes: {data == on_disk}")
            print(f"  sha256 tool == disk: {p['sha256'] == hashlib.sha256(on_disk).hexdigest()}")
            print("  xyz head:", on_disk.decode().splitlines()[0:2])


asyncio.run(main())
