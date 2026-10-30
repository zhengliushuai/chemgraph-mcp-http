"""HTTP artifact download endpoint for the ChemGraph MCP server.

Mounted on the same uvicorn app that serves MCP, so a remote ChemGraph host can
pull large calculation products (traj, cube, csv, log ...) with curl/requests
instead of dragging base64 through its LLM context.

Routes
------
``GET  /artifacts/{path}``            file body, supports ``Range`` and
                                      ``If-None-Match`` (ETag), plus
                                      ``?sha256=1`` for a checksum-only probe
``HEAD /artifacts/{path}``            headers only

Same containment rules as the MCP tool: everything is confined to
``CHEMGRAPH_DIR``, files above 1 GiB are refused (HTTP 413) rather than
truncated.
"""

from __future__ import annotations

import json
import os
from typing import Iterator

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from artifact_tools import (
    MAX_ARTIFACT_BYTES,
    check_size,
    guess_mime,
    resolve_artifact,
    sha256_of_range,
)

_CHUNK = 1024 * 1024


def _error(exc: Exception) -> Response:
    """Map a resolver exception onto an HTTP status."""
    if isinstance(exc, PermissionError):
        return Response(str(exc), status_code=403)
    if isinstance(exc, FileNotFoundError):
        return Response(str(exc), status_code=404)
    if isinstance(exc, IsADirectoryError):
        return Response(str(exc), status_code=400)
    if isinstance(exc, ValueError):
        return Response(str(exc), status_code=413)
    return Response(f"internal error: {exc}", status_code=500)


def _parse_range(header: str, size: int) -> tuple[int, int]:
    """Parse a single ``bytes=start-end`` Range header.

    Returns
    -------
    tuple of int
        Inclusive ``(start, end)`` byte offsets.

    Raises
    ------
    ValueError
        If the header is malformed or unsatisfiable (caller maps to 416).
    """
    spec = header.strip()
    if not spec.lower().startswith("bytes="):
        raise ValueError(f"unsupported Range header: {header!r}")
    spec = spec[6:].split(",", 1)[0].strip()  # only the first range
    start_s, _, end_s = spec.partition("-")
    try:
        if not start_s:  # suffix range: bytes=-500
            suffix = int(end_s)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        raise ValueError(f"malformed Range header: {header!r}") from None
    if start >= size or start > end:
        raise ValueError(f"unsatisfiable Range: {header!r} for size {size}")
    return start, min(end, size - 1)


def _iter_file(path: str, start: int, length: int) -> Iterator[bytes]:
    """Yield ``length`` bytes of *path* from *start*."""
    remaining = length
    with open(path, "rb") as fh:
        if start:
            fh.seek(start)
        while remaining > 0:
            block = fh.read(min(_CHUNK, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


def artifacts_endpoint(request: Request) -> Response:
    """Serve one artifact, honoring Range/ETag, or a checksum probe."""
    raw_path = request.path_params.get("path", "")
    try:
        target = resolve_artifact(raw_path)
        size = check_size(target)
    except Exception as exc:  # noqa: BLE001 - mapped onto HTTP statuses
        return _error(exc)

    stat = target.stat()
    etag = f'"{stat.st_size:x}-{int(stat.st_mtime):x}"'

    # ?sha256=1 -> metadata only, streamed hash of the whole file.
    if request.query_params.get("sha256") in ("1", "true", "yes"):
        body = {
            "filename": target.name,
            "size_bytes": size,
            "sha256": sha256_of_range(target, 0, size),
            "mime": guess_mime(target.name),
        }
        return Response(json.dumps(body), media_type="application/json")

    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Content-Disposition": f'attachment; filename="{target.name}"',
        "X-Artifact-Size": str(size),
    }

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    range_header = request.headers.get("range")
    if range_header:
        try:
            start, end = _parse_range(range_header, size)
        except ValueError as exc:
            return Response(
                str(exc),
                status_code=416,
                headers={"Content-Range": f"bytes */{size}", **headers},
            )
        length = end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(length)
        if request.method == "HEAD":
            return Response(status_code=206, headers=headers)
        return StreamingResponse(
            _iter_file(str(target), start, length),
            status_code=206,
            media_type=guess_mime(target.name),
            headers=headers,
        )

    headers["Content-Length"] = str(size)
    if request.method == "HEAD":
        return Response(headers=headers)
    return StreamingResponse(
        _iter_file(str(target), 0, size),
        media_type=guess_mime(target.name),
        headers=headers,
    )


#: Route to insert into the MCP app's router.
artifact_route = Route(
    "/artifacts/{path:path}",
    artifacts_endpoint,
    methods=["GET", "HEAD"],
)
