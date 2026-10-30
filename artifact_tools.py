"""Artifact read/download support for the ChemGraph MCP HTTP server.

Adds one MCP tool (:func:`read_artifact`) plus the shared, security-checked path
resolution used by the ``GET /artifacts/{path}`` HTTP endpoint in
:mod:`http_artifacts`.

Every path is confined to the ``CHEMGRAPH_DIR`` root (falling back to
``CHEMGRAPH_LOG_DIR``, then the server's working directory). Anything that
escapes that root -- ``..`` traversal, an absolute path elsewhere, a symlink or
NTFS junction pointing outside -- is rejected, never silently clamped.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Any

#: Hard ceiling. Larger files raise instead of being truncated.
MAX_ARTIFACT_BYTES: int = 1024**3  # 1 GiB

#: How much content the MCP tool will inline as base64 by default. Tool results
#: land in the *client's* LLM context, so a multi-MB file would blow it up.
#: ``force_base64=True`` overrides this; the 1 GiB ceiling still applies.
DEFAULT_BASE64_LIMIT: int = 8 * 1024**2  # 8 MiB

_HASH_CHUNK = 8 * 1024 * 1024
_READ_CHUNK = 1024 * 1024

_MIME_OVERRIDES = {
    ".xyz": "chemical/x-xyz",
    ".traj": "chemical/x-ase-trajectory",
    ".cif": "chemical/x-cif",
    ".pdb": "chemical/x-pdb",
    ".cube": "chemical/x-cube",
    ".extxyz": "chemical/x-extxyz",
    ".json": "application/json",
    ".csv": "text/csv",
    ".log": "text/plain",
    ".out": "text/plain",
    ".py": "text/x-python",
}

# ---------------------------------------------------------------------------
# Path resolution / containment
# ---------------------------------------------------------------------------


def artifact_root() -> Path:
    """Return the directory artifacts are confined to.

    Honors ``CHEMGRAPH_DIR``, then ``CHEMGRAPH_LOG_DIR``, then the process CWD.
    """
    raw = (
        os.environ.get("CHEMGRAPH_DIR")
        or os.environ.get("CHEMGRAPH_LOG_DIR")
        or os.getcwd()
    )
    return Path(os.path.realpath(os.path.expanduser(raw)))


def _is_within(child: str, root: str) -> bool:
    """Case-insensitive containment test (Windows paths are case-insensitive)."""
    c = os.path.normcase(child)
    r = os.path.normcase(root)
    return c == r or c.startswith(r + os.sep)


def resolve_artifact(path: str) -> Path:
    """Resolve *path* to a real file path inside :func:`artifact_root`.

    Parameters
    ----------
    path : str
        Relative path (resolved against the artifact root) or absolute path.

    Returns
    -------
    Path
        Real, root-contained absolute path.

    Raises
    ------
    PermissionError
        If the resolved target is outside the artifact root.
    FileNotFoundError
        If the target does not exist.
    IsADirectoryError
        If the target is a directory.
    """
    root = artifact_root()
    raw = Path(os.path.expanduser(path))
    candidate = raw if raw.is_absolute() else root / raw
    real = os.path.realpath(str(candidate))

    if not _is_within(real, str(root)):
        raise PermissionError(
            f"path outside artifact root: {path!r} resolves to {real!r}, "
            f"root is {str(root)!r}"
        )
    if not os.path.exists(real):
        raise FileNotFoundError(
            f"artifact not found: {path!r} (artifact root: {str(root)!r})"
        )
    if os.path.isdir(real):
        raise IsADirectoryError(f"artifact path is a directory, not a file: {real!r}")
    if not os.path.isfile(real):
        raise FileNotFoundError(f"artifact is not a regular file: {real!r}")
    return Path(real)


def check_size(path: Path) -> int:
    """Return the size of *path*, raising if it exceeds :data:`MAX_ARTIFACT_BYTES`."""
    size = path.stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        raise ValueError(
            f"file too large: {path.name} is {size} bytes, limit is "
            f"{MAX_ARTIFACT_BYTES} bytes (1 GiB). Refusing to read it -- "
            "nothing was truncated."
        )
    return size


def guess_mime(name: str) -> str:
    """Best-effort MIME type for an artifact filename."""
    suffix = Path(name).suffix.lower()
    if suffix in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[suffix]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def sha256_of_range(path: Path, offset: int, length: int) -> str:
    """SHA-256 of a byte range of *path*, streamed in chunks."""
    digest = hashlib.sha256()
    remaining = length
    with open(path, "rb") as fh:
        if offset:
            fh.seek(offset)
        while remaining > 0:
            block = fh.read(min(_HASH_CHUNK, remaining))
            if not block:
                break
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# read_artifact
# ---------------------------------------------------------------------------


def read_artifact_core(
    path: str,
    force_base64: bool = False,
    offset: int = 0,
    length: int = 0,
    base64_limit: int | None = None,
) -> dict[str, Any]:
    """Read an artifact and return its metadata plus (optionally) base64 content.

    Parameters
    ----------
    path : str
        Artifact path, relative to the artifact root or absolute inside it.
    force_base64 : bool, optional
        Inline base64 even when the payload exceeds the context guard.
    offset : int, optional
        Byte offset to start reading from (default 0).
    length : int, optional
        Number of bytes to read; 0 or negative means "to end of file".
    base64_limit : int, optional
        Override :data:`DEFAULT_BASE64_LIMIT` for this call.

    Returns
    -------
    dict
        ``filename``, ``size_bytes``, ``sha256``, ``content_base64`` plus
        supporting metadata. ``content_base64`` is ``None`` when the payload is
        withheld by the context guard; ``note`` then explains how to fetch it.
    """
    limit = DEFAULT_BASE64_LIMIT if base64_limit is None else base64_limit
    target = resolve_artifact(path)
    size = check_size(target)

    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")
    if offset > size:
        raise ValueError(
            f"offset {offset} is beyond end of file (size {size} bytes)"
        )
    span = size - offset if length <= 0 else min(length, size - offset)

    digest = sha256_of_range(target, offset, span)

    def payload_note(nbytes: int) -> str | None:
        if not force_base64 and nbytes > limit:
            return (
                f"content withheld: {nbytes} bytes exceeds the {limit}-byte inline "
                "limit (keeps this result out of the client's LLM context). "
                f"Fetch it with GET /artifacts/{target.name}, or pass "
                "force_base64=true, or read it in chunks with offset/length."
            )
        return None

    note = payload_note(span)
    content_b64: str | None = None
    if note is None:
        with open(target, "rb") as fh:
            if offset:
                fh.seek(offset)
            data = fh.read(span)
        content_b64 = base64.b64encode(data).decode("ascii")

    return {
        "filename": target.name,
        "size_bytes": size,
        "sha256": digest,
        "content_base64": content_b64,
        "encoding": "base64",
        "mime": guess_mime(target.name),
        "server_path": str(target),
        "range": {"offset": offset, "length": span},
        "sha256_scope": "range" if (offset or span != size) else "whole_file",
        "note": note,
    }


def register_artifact_tools(mcp: Any) -> None:
    """Attach the ``read_artifact`` tool to a FastMCP server instance."""

    @mcp.tool(
        name="read_artifact",
        description=(
            "Read a file produced on the server (xyz, traj, json, csv, log ...) and "
            "return {filename, size_bytes, sha256, content_base64}. Only files under "
            "the server's CHEMGRAPH_DIR are readable. Files larger than 1 GiB are "
            "refused, never truncated. Payloads above 8 MiB come back as metadata "
            "only unless force_base64=true; for big files prefer downloading "
            "GET /artifacts/<filename> from the same host/port."
        ),
    )
    async def read_artifact(
        path: str,
        force_base64: bool = False,
        offset: int = 0,
        length: int = 0,
    ) -> dict[str, Any]:
        """Read a server-side artifact.

        Parameters
        ----------
        path : str
            Path of the artifact, relative to the server's artifact root (for
            example "ethanol_opt.traj") or an absolute path inside that root.
        force_base64 : bool, optional
            Inline base64 even when the file is larger than the 8 MiB inline
            limit. Use sparingly -- the payload enters the model context.
        offset : int, optional
            Byte offset to start from, for chunked reads of large files.
        length : int, optional
            Bytes to read from `offset`; 0 means "to end of file".

        Returns
        -------
        dict
            filename, size_bytes, sha256 (of the returned range),
            content_base64, plus mime/server_path/range/note.
        """
        return read_artifact_core(
            path,
            force_base64=force_base64,
            offset=offset,
            length=length,
        )
