"""Serve any ChemGraph MCP server over streamable HTTP for remote clients.

Why this exists: ChemGraph's own ``server_utils.run_mcp_server`` builds the
FastMCP app with the SDK defaults (``allowed_hosts=["127.0.0.1:*",
"localhost:*", "[::1]:*"]``), so any request from another machine is answered
with ``421 Invalid Host header``. This launcher rebuilds the same app with the
DNS-rebinding host allow-list relaxed.

Usage:
    python mcp_http_server.py --module chemgraph.mcp.mcp_tools --port 9003
    python mcp_http_server.py --module chemgraph.mcp.mcp_tools --port 9003 \
        --allow-host 10.0.0.5:9003
"""

from __future__ import annotations

import argparse
import importlib

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

from artifact_tools import artifact_root, register_artifact_tools
from http_artifacts import artifact_route


def main() -> None:
    ap = argparse.ArgumentParser(description="ChemGraph MCP server over HTTP")
    ap.add_argument("--module", default="chemgraph.mcp.mcp_tools",
                    help="dotted module exposing a module-level `mcp` FastMCP object")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=9003, help="port (default 9003)")
    ap.add_argument("--allow-host", action="append", default=[],
                    help="Host header value to accept, repeatable, e.g. 10.0.0.5:9003. "
                         "Omit to disable the host check entirely.")
    args = ap.parse_args()

    mod = importlib.import_module(args.module)
    mcp = mod.mcp

    if args.allow_host:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(args.allow_host),
        )
        print(f"[mcp-http] host allow-list: {args.allow_host}", flush=True)
    else:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
        print("[mcp-http] DNS-rebinding host check disabled", flush=True)

    register_artifact_tools(mcp)
    app = mcp.streamable_http_app()
    app.router.routes.insert(0, artifact_route)
    print(f"[mcp-http] artifact root: {str(artifact_root())} "
          f"(GET /artifacts/<name>, 1 GiB ceiling)", flush=True)
    print(f"[mcp-http] serving {mcp.name!r} on http://{args.host}:{args.port}/mcp",
          flush=True)
    uvicorn.run(app, host=args.host, port=args.port, ws="none")


if __name__ == "__main__":
    main()
