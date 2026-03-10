"""Direct CAS (Content Addressable Storage) access via the RBE REST API.

Avoids spawning `cas download` subprocesses and downloading full isolate
trees.  Algorithm:

  Phase 1 — BFS across all isolates, level by level:
    Fetch directory blobs via BatchReadBlobs (binary proto), parse with
    Directory.FromString(), extract FileNode/DirectoryNode digests.
    All unique directory blobs at each BFS level are batched into as few
    API calls as possible (with deduplication across isolates).
    Stop descending a branch as soon as the probe file is found.

  Phase 2 — BatchReadBlobs for all probe file blobs:
    Collect all found file digests, fetch contents in one batched call.

Auth uses Application Default Credentials (gcloud auth application-default
login).
"""

from __future__ import annotations

import base64
import logging

import httpx
from google.auth import default as _gauth_default
from google.auth.transport.requests import Request as _AuthRequest

import rbe_pb2

log = logging.getLogger(__name__)

_RBE_BASE = "https://remotebuildexecution.googleapis.com/v2"
_CAS_INSTANCE = "projects/chrome-swarming/instances/default_instance"
_BATCH_SIZE = 100  # max digests per BatchReadBlobs call


# ── Auth ──────────────────────────────────────────────────────────────────────

_creds = None


def _auth_headers() -> dict[str, str]:
    global _creds
    if _creds is None:
        log.debug("loading Application Default Credentials")
        _creds, _ = _gauth_default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    log.debug("refreshing auth token")
    _creds.refresh(_AuthRequest())
    return {"Authorization": f"Bearer {_creds.token}"}


def _parse_digest(d: str) -> tuple[str, int]:
    h, _, s = d.partition("/")
    return h, int(s)


# ── Low-level RBE helper ──────────────────────────────────────────────────────

def _batch_read_blobs(
    client: httpx.Client,
    digests: list[tuple[str, int]],
) -> dict[str, bytes]:
    """Fetch multiple blobs in batches.  Returns {hash: raw_bytes}."""
    result: dict[str, bytes] = {}
    for i in range(0, len(digests), _BATCH_SIZE):
        batch = digests[i : i + _BATCH_SIZE]
        log.debug("BatchReadBlobs: %d blobs (batch %d-%d of %d)",
                  len(batch), i + 1, min(i + _BATCH_SIZE, len(digests)), len(digests))
        payload = {
            "digests": [{"hash": h, "sizeBytes": str(s)} for h, s in batch]
        }
        r = client.post(
            f"{_RBE_BASE}/{_CAS_INSTANCE}/blobs:batchRead",
            json=payload,
        )
        r.raise_for_status()
        errors = 0
        for resp in r.json().get("responses", []):
            code = resp.get("status", {}).get("code", 0)
            if code != 0:
                log.debug("  blob %s: error code %d", resp["digest"]["hash"][:12], code)
                errors += 1
                continue
            result[resp["digest"]["hash"]] = base64.b64decode(resp["data"])
        if errors:
            log.debug("  %d/%d blobs returned errors", errors, len(batch))
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_probe_files(
    root_digests: list[str],
    probe_filename: str,
) -> list[bytes | None]:
    """Fetch probe JSON bytes for each CAS root digest.

    root_digests:   list of "sha256hash/size" strings (one per bot run)
    probe_filename: filename to search for, e.g. "jetstream_main.json"

    Returns a list parallel to root_digests; each entry is the raw file bytes
    or None if the file was not found or the fetch failed.

    BFS walks the directory tree for all isolates in parallel, deduplicating
    identical directory blobs across isolates.  All blob fetches are batched.
    """
    headers = _auth_headers()

    # remaining[root_digest] = list of (hash, size) directory blobs still to explore
    remaining: dict[str, list[tuple[str, int]]] = {
        d: [_parse_digest(d)] for d in root_digests
    }
    # file_digest[root_digest] = (hash, size) once found, else None
    file_digest: dict[str, tuple[str, int] | None] = {d: None for d in root_digests}
    # cache of already-fetched directory blobs
    dir_cache: dict[tuple[str, int], rbe_pb2.Directory] = {}

    n_total = len(root_digests)
    log.debug("starting BFS for %r across %d isolates", probe_filename, n_total)

    with httpx.Client(headers=headers, timeout=60) as client:
        bfs_level = 0
        while True:
            # Collect unique directory blobs needed at this BFS level
            needed: set[tuple[str, int]] = set()
            pending = 0
            for root_digest, dirs in remaining.items():
                if file_digest[root_digest] is not None:
                    continue
                pending += 1
                for key in dirs:
                    if key not in dir_cache:
                        needed.add(key)

            if not needed:
                break

            log.debug("BFS level %d: fetching %d unique dir blobs (%d isolates still searching)",
                      bfs_level, len(needed), pending)

            # Fetch and parse all needed directory blobs
            raw_blobs = _batch_read_blobs(client, list(needed))
            parse_errors = 0
            for h, s in needed:
                raw = raw_blobs.get(h)
                if raw is not None:
                    try:
                        dir_cache[(h, s)] = rbe_pb2.Directory.FromString(raw)
                    except Exception as e:
                        log.debug("  failed to parse directory %s: %s", h[:12], e)
                        parse_errors += 1
                else:
                    log.debug("  directory blob %s not returned by server", h[:12])
            if parse_errors:
                log.debug("  %d parse errors at this level", parse_errors)

            # Advance BFS for each root
            next_remaining: dict[str, list[tuple[str, int]]] = {
                d: [] for d in root_digests
            }
            found_this_level = 0
            for root_digest, dirs in remaining.items():
                if file_digest[root_digest] is not None:
                    continue
                for key in dirs:
                    d = dir_cache.get(key)
                    if d is None:
                        continue
                    for fn in d.files:
                        if fn.name == probe_filename:
                            file_digest[root_digest] = (
                                fn.digest.hash, fn.digest.size_bytes
                            )
                            found_this_level += 1
                            break
                    if file_digest[root_digest] is not None:
                        break
                    for dn in d.directories:
                        next_remaining[root_digest].append(
                            (dn.digest.hash, dn.digest.size_bytes)
                        )

            if found_this_level:
                log.debug("  found %r in %d isolate(s) at level %d",
                          probe_filename, found_this_level, bfs_level)

            remaining = next_remaining
            bfs_level += 1
            if not any(remaining.values()):
                break

        found_total = sum(1 for fd in file_digest.values() if fd is not None)
        log.debug("BFS complete: found file in %d/%d isolates", found_total, n_total)

        # Batch-fetch all found file blobs
        file_digests_needed = [
            fd for fd in file_digest.values() if fd is not None
        ]
        deduped = list({(h, s) for h, s in file_digests_needed})
        log.debug("fetching %d unique file blobs (%d total)", len(deduped), len(file_digests_needed))
        blob_by_hash: dict[str, bytes] = (
            _batch_read_blobs(client, deduped) if deduped else {}
        )
        log.debug("received %d file blobs", len(blob_by_hash))

    out: list[bytes | None] = []
    for root_digest in root_digests:
        fd = file_digest.get(root_digest)
        out.append(blob_by_hash.get(fd[0]) if fd else None)
    return out
