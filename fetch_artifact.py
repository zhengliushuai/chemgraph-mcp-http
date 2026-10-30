#!/usr/bin/env python3
"""Fetch an artifact from a remote ChemGraph MCP server and verify its checksum.

Runs on the *client* machine with the standard library only (no pip install).

    python fetch_artifact.py http://10.0.0.5:9003 ethanol_opt.traj
    python fetch_artifact.py http://10.0.0.5:9003 ethanol_opt.traj -o ./out/
    python fetch_artifact.py http://10.0.0.5:9003 sub/dir/run.log --quiet

Flow: ask `/artifacts/<name>?sha256=1` for {size, sha256} -> stream the body to a
`.part` file -> hash it locally -> compare -> rename into place. A mismatch
deletes the temp file and exits 1, so a half-transferred file never looks done.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

CHUNK = 1024 * 1024


def _get(url: str):
    return urllib.request.urlopen(url, timeout=60)


def fetch_metadata(base: str, name: str) -> dict:
    """Return {'size_bytes', 'sha256', 'filename'} from the server."""
    url = f"{base.rstrip('/')}/artifacts/{urllib.parse.quote(name)}?sha256=1"
    with _get(url) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download(base: str, name: str, dest: str, quiet: bool = False) -> tuple[str, int]:
    """Stream the artifact to *dest*. Returns (local_sha256, bytes_written)."""
    url = f"{base.rstrip('/')}/artifacts/{urllib.parse.quote(name)}"
    digest = hashlib.sha256()
    written = 0
    tmp = dest + ".part"
    with _get(url) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length", 0) or 0)
        while True:
            block = resp.read(CHUNK)
            if not block:
                break
            fh.write(block)
            digest.update(block)
            written += len(block)
            if not quiet and total:
                pct = 100.0 * written / total
                print(f"\r  {written}/{total} bytes ({pct:5.1f}%)", end="", flush=True)
    if not quiet and sys.stdout.isatty():
        print()
    return digest.hexdigest(), written


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch an artifact from a ChemGraph MCP server")
    ap.add_argument("base", help="server base URL, e.g. http://10.0.0.5:9003")
    ap.add_argument("name", help="artifact path as seen by the server, e.g. ethanol_opt.traj")
    ap.add_argument("-o", "--output", default=None,
                    help="destination file or directory (default: ./<basename>)")
    ap.add_argument("--quiet", action="store_true", help="no progress output")
    args = ap.parse_args()

    dest = args.output or os.path.basename(args.name.rstrip("/"))
    if dest.endswith(("/", "\\")) or os.path.isdir(dest):
        os.makedirs(dest, exist_ok=True)
        dest = os.path.join(dest, os.path.basename(args.name))
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)

    try:
        meta = fetch_metadata(args.base, args.name)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace").strip()
        print(f"error: server returned {exc.code} for {args.name}: {body}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"error: cannot reach {args.base}: {exc.reason}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"server says: {meta['filename']} {meta['size_bytes']} bytes "
              f"sha256={meta['sha256']}")

    try:
        local_sha, written = download(args.base, args.name, dest, quiet=args.quiet)
    except urllib.error.HTTPError as exc:
        print(f"error: download failed with {exc.code}: "
              f"{exc.read().decode('utf-8', 'replace').strip()}", file=sys.stderr)
        return 1

    ok = local_sha == meta["sha256"] and written == meta["size_bytes"]
    if not ok:
        print(f"error: checksum mismatch\n  server {meta['sha256']} ({meta['size_bytes']} B)"
              f"\n  local  {local_sha} ({written} B)", file=sys.stderr)
        try:
            os.remove(dest + ".part")
        except OSError:
            pass
        return 1

    os.replace(dest + ".part", dest)
    if not args.quiet:
        print(f"OK -> {dest} ({written} bytes, sha256 verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
