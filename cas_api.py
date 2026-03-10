"""Direct CAS (Content Addressable Storage) access via the RBE REST API.

Avoids spawning `cas download` subprocesses and downloading full isolate
trees.  Instead:

  1. GetTree  — one call per isolate — returns all Directory protos in the
               tree, which we hash-index to reconstruct the path structure.
  2. Find the probe file by filename anywhere in the tree (rglob-equivalent).
  3. BatchReadBlobs — one call for ALL file blobs across all isolates — to
               fetch the actual probe JSON content.

Auth uses Application Default Credentials (gcloud auth application-default
login), the same mechanism as the `cas` CLI.
"""

from __future__ import annotations

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx
from google.auth import default as _gauth_default
from google.auth.transport.requests import Request as _AuthRequest

import rbe_pb2

_RBE_BASE = "https://remotebuildexecution.googleapis.com/v2"
_CAS_INSTANCE = "projects/chrome-swarming/instances/default_instance"

# BatchReadBlobs accepts up to 4 MB total; we stay well under with 100 at a time.
_BATCH_SIZE = 100


# ── Auth ──────────────────────────────────────────────────────────────────────

_creds = None


def _auth_headers() -> dict[str, str]:
    global _creds
    if _creds is None:
        _creds, _ = _gauth_default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    _creds.refresh(_AuthRequest())
    return {"Authorization": f"Bearer {_creds.token}"}


# ── Low-level RBE helpers ─────────────────────────────────────────────────────

def _get_tree(client: httpx.Client, root_hash: str, root_size: int) -> list[rbe_pb2.Directory]:
    """Return all Directory protos in a CAS tree (handles pagination)."""
    dirs: list[rbe_pb2.Directory] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {"pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
        r = client.get(
            f"{_RBE_BASE}/{_CAS_INSTANCE}/blobs/{root_hash}/{root_size}:getTree",
            params=params,
        )
        r.raise_for_status()
        data = r.json()
        for raw in data.get("directories", []):
            d = rbe_pb2.Directory()
            # Parse from JSON using proto3 field names.
            for fn in raw.get("files", []):
                fn_msg = d.files.add()
                fn_msg.name = fn["name"]
                fn_msg.digest.hash = fn["digest"]["hash"]
                fn_msg.digest.size_bytes = int(fn["digest"]["sizeBytes"])
            for dn in raw.get("directories", []):
                dn_msg = d.directories.add()
                dn_msg.name = dn["name"]
                dn_msg.digest.hash = dn["digest"]["hash"]
                dn_msg.digest.size_bytes = int(dn["digest"]["sizeBytes"])
            dirs.append(d)
        page_token = data.get("nextPageToken", "")
        if not page_token:
            break
    return dirs


def _batch_read_blobs(
    client: httpx.Client,
    digests: list[tuple[str, int]],
) -> dict[str, bytes]:
    """Fetch multiple blobs in batches.  Returns {hash: raw_bytes}."""
    result: dict[str, bytes] = {}
    for i in range(0, len(digests), _BATCH_SIZE):
        batch = digests[i : i + _BATCH_SIZE]
        payload = {
            "digests": [{"hash": h, "sizeBytes": str(s)} for h, s in batch]
        }
        r = client.post(
            f"{_RBE_BASE}/{_CAS_INSTANCE}/blobs:batchRead",
            json=payload,
        )
        r.raise_for_status()
        for resp in r.json().get("responses", []):
            status = resp.get("status", {})
            if status.get("code", 0) != 0:
                continue  # skip missing/error blobs
            h = resp["digest"]["hash"]
            result[h] = base64.b64decode(resp["data"])
    return result


# ── Tree indexing ─────────────────────────────────────────────────────────────

def _index_tree(dirs: list[rbe_pb2.Directory]) -> dict[str, rbe_pb2.Directory]:
    """Build {sha256_hash: Directory} by hashing each directory's serialized form."""
    index: dict[str, rbe_pb2.Directory] = {}
    for d in dirs:
        h = hashlib.sha256(d.SerializeToString()).hexdigest()
        index[h] = d
    return index


def _find_files(
    root_hash: str,
    index: dict[str, rbe_pb2.Directory],
    filename: str,
) -> list[tuple[str, int]]:
    """Return digests of all files named `filename` anywhere under root_hash.

    Traverses the directory tree via BFS; returns [(hash, size_bytes), ...].
    Returns the shallowest match first (minimum depth).
    """
    results: list[tuple[int, str, int]] = []  # (depth, hash, size)
    queue: list[tuple[int, str]] = [(0, root_hash)]
    visited: set[str] = set()

    while queue:
        depth, dh = queue.pop(0)
        if dh in visited:
            continue
        visited.add(dh)
        d = index.get(dh)
        if d is None:
            continue
        for fn in d.files:
            if fn.name == filename:
                results.append((depth, fn.digest.hash, fn.digest.size_bytes))
        for dn in d.directories:
            queue.append((depth + 1, dn.digest.hash))

    results.sort(key=lambda t: t[0])
    return [(h, s) for _, h, s in results]


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_probe_files(
    root_digests: list[str],
    probe_filename: str,
) -> list[bytes | None]:
    """Fetch probe JSON bytes for each CAS root digest.

    root_digests: list of "sha256hash/size" strings (one per bot run)
    probe_filename: filename to search for, e.g. "jetstream_main.json"

    Returns a list parallel to root_digests; each entry is the raw file bytes
    or None if the file was not found / the fetch failed.

    All tree fetches run in parallel; all blob fetches are batched into as few
    API calls as possible.
    """
    headers = _auth_headers()

    def _parse_digest(d: str) -> tuple[str, int]:
        h, _, s = d.partition("/")
        return h, int(s)

    # Phase 1: fetch all directory trees in parallel
    def _fetch_tree(root_digest: str) -> tuple[str, dict[str, rbe_pb2.Directory] | None]:
        try:
            h, s = _parse_digest(root_digest)
            with httpx.Client(headers=headers, timeout=30) as c:
                dirs = _get_tree(c, h, s)
            return root_digest, _index_tree(dirs)
        except Exception:
            return root_digest, None

    tree_by_root: dict[str, dict[str, rbe_pb2.Directory] | None] = {}
    with ThreadPoolExecutor(max_workers=20) as pool:
        for root_digest, index in pool.map(_fetch_tree, root_digests):
            tree_by_root[root_digest] = index

    # Phase 2: find the probe file digest for each root
    # file_digest_hash → (hash, size) for BatchReadBlobs
    file_digest_by_root: dict[str, tuple[str, int] | None] = {}
    all_file_digests: list[tuple[str, int]] = []

    for root_digest in root_digests:
        index = tree_by_root.get(root_digest)
        if index is None:
            file_digest_by_root[root_digest] = None
            continue
        root_hash, _ = _parse_digest(root_digest)
        matches = _find_files(root_hash, index, probe_filename)
        if not matches:
            file_digest_by_root[root_digest] = None
        else:
            fd = matches[0]  # shallowest match
            file_digest_by_root[root_digest] = fd
            all_file_digests.append(fd)

    if not all_file_digests:
        return [None] * len(root_digests)

    # Phase 3: batch-fetch all file blobs in one (or a few) API calls
    deduped = list({(h, s) for h, s in all_file_digests})
    with httpx.Client(headers=headers, timeout=60) as c:
        blob_by_hash = _batch_read_blobs(c, deduped)

    # Assemble results in input order
    out: list[bytes | None] = []
    for root_digest in root_digests:
        fd = file_digest_by_root.get(root_digest)
        if fd is None:
            out.append(None)
        else:
            out.append(blob_by_hash.get(fd[0]))
    return out
