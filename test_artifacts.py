"""Acceptance tests for read_artifact + /artifacts over the real MCP protocol."""

import asyncio
import base64
import hashlib
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = os.environ.get("MCP_BASE", "http://127.0.0.1:9003")
ROOT = os.environ.get("CHEMGRAPH_DIR") or os.getcwd()
FIXTURE = "ethanol_opt.traj"   # self-seeded if missing, so a fresh server can run these tests


def local_sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


async def call(session, **kw):
    res = await session.call_tool("read_artifact", kw)
    text = res.content[0].text if res.content else ""
    try:
        payload = json.loads(text)
    except Exception:
        payload = {"raw": text}
    return res.isError, payload


async def main():
    async with streamablehttp_client(f"{BASE}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            print("TOOLS:", tools)

            # 0. make sure the fixture exists (empty server -> ask the server to build it)
            if not os.path.exists(os.path.join(ROOT, FIXTURE)):
                made = await s.call_tool("smiles_to_coordinate_file",
                                         {"smiles": "CCO", "output_file": FIXTURE})
                print("seeded fixture:", FIXTURE, "| isError:", made.isError)

            # 1. small file round-trip
            err, p = await call(s, path=FIXTURE)
            data = base64.b64decode(p["content_base64"]) if p.get("content_base64") else b""
            print(f"[1] small: err={err} name={p.get('filename')} size={p.get('size_bytes')} "
                  f"b64_bytes={len(data)} sha_ok={p.get('sha256') == hashlib.sha256(data).hexdigest()} "
                  f"file_sha_ok={p.get('sha256') == local_sha(os.path.join(ROOT, FIXTURE))} "
                  f"note={p.get('note')}")

            # 2. traversal / absolute / symlink escapes
            for bad in ["..\\..\\Windows\\win.ini", "C:\\Windows\\win.ini",
                        "../../../../etc/hosts", "cg_logs/../../ethanol.xyz"]:
                err, p = await call(s, path=bad)
                print(f"[2] reject {bad!r}: err={err} msg={(p.get('raw') or '')[:90]!r}")

            # 3. missing file
            err, p = await call(s, path="does_not_exist.xyz")
            print(f"[3] missing: err={err} msg={(p.get('raw') or '')[:80]!r}")

            # 4. directory (ensure one exists on a fresh server)
            os.makedirs(os.path.join(ROOT, "cg_logs"), exist_ok=True)
            err, p = await call(s, path="cg_logs")
            print(f"[4] directory: err={err} msg={(p.get('raw') or '')[:80]!r}")

            # 5. 1.1 GiB sparse file -> must raise, not truncate
            big = os.path.join(ROOT, "_big_1_1GiB.bin")
            with open(big, "wb") as f:
                f.truncate(1126 * 1024 * 1024)
            err, p = await call(s, path="_big_1_1GiB.bin")
            print(f"[5] >1GiB: err={err} msg={(p.get('raw') or '')[:130]!r}")
            os.remove(big)

            # 6. 12 MiB file: metadata-only by default, base64 with force_base64
            mid = os.path.join(ROOT, "_mid_12MiB.bin")
            with open(mid, "wb") as f:
                f.write(b"A" * (12 * 1024 * 1024))
            err, p = await call(s, path="_mid_12MiB.bin")
            print(f"[6a] 12MiB default: b64={p.get('content_base64')} note={(p.get('note') or '')[:70]!r}")
            err, p = await call(s, path="_mid_12MiB.bin", force_base64=True)
            print(f"[6b] 12MiB forced: err={err} b64_len={len(p.get('content_base64') or '')} "
                  f"roundtrip_ok={len(base64.b64decode(p['content_base64'])) == 12 * 1024 * 1024}")

            # 7. chunked read
            err, p = await call(s, path="_mid_12MiB.bin", offset=1000, length=64)
            chunk = base64.b64decode(p["content_base64"])
            print(f"[7] chunk: len={len(chunk)} all_A={chunk == b'A' * 64} "
                  f"range={p.get('range')} scope={p.get('sha256_scope')}")
            os.remove(mid)


asyncio.run(main())
