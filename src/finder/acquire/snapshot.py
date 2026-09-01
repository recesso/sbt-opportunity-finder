"""Content-addressed snapshot store — the audit trail.

Every extraction reads from here and never from the network. That single rule is
what makes three things possible:

* **Reproducibility.** An extraction can be replayed months later against the
  exact bytes it saw, offline.
* **The independent audit (E5.S6).** A second model re-extracts from the same
  snapshot. Without stored bytes there is nothing to compare against.
* **Detecting fabrication.** A span either appears in the stored text or it does
  not. With a live handle there is no ground truth to check against.

The store is append-only by construction. There is no delete method, and there
is no code path that rewrites an existing file — the same guarantee `MarkRepo`
gives founder data, for the same reason: what an audit trail records must not
depend on someone remembering not to change it.

Layout::

    data/snapshots/<first two hex chars>/<sha256>.md.gz

The two-character shard is a deliberate deviation from the one-line spec in the
backlog (`data/snapshots/<sha256>.md.gz`). Snapshots are retained forever and a
weekly run stores hundreds; a flat directory reaches five figures within months
and degrades badly on NTFS. The shard costs one line and nothing else.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

SUFFIX = ".md.gz"
_SHARD_LEN = 2
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_WHITESPACE = re.compile(r"\s+")


class SnapshotError(Exception):
    """A snapshot could not be read, or would have been altered."""


def normalize(text: str) -> str:
    """Collapse whitespace so a reflowed page is the same page.

    Providers re-wrap markdown between calls. Hashing the raw bytes would treat
    every re-wrap as new content and store the same page a dozen times, which
    defeats both the cache and the audit.
    """
    return _WHITESPACE.sub(" ", text).strip()


def content_hash(text: str) -> str:
    """The identity of a page: sha256 over its whitespace-normalised text."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StoreStats:
    count: int
    bytes_on_disk: int


class SnapshotStore:
    """Write-once storage keyed by content hash.

    ``put`` is idempotent: storing the same page twice writes once. Storing
    *different* text under a hash that already exists raises rather than
    overwriting — a silent overwrite would rewrite history, and history is the
    only thing this class is for.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # --- writing ----------------------------------------------------------

    def put(self, text: str) -> str:
        """Store the text and return its content hash.

        Written to a temporary file and moved into place, so a process killed
        mid-write leaves either nothing or a complete file — never a truncated
        archive that reads as a valid but shorter page.
        """
        digest = content_hash(text)
        path = self.path_for(digest)

        if path.exists():
            if normalize(self._read(path)) != normalize(text):
                raise SnapshotError(
                    f"snapshot {digest} already holds different content; refusing to "
                    "overwrite. Two pages cannot share a hash, so this is corruption "
                    "or a bug in the hashing."
                )
            return digest

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            # mtime=0 so the same text always produces the same bytes; a store
            # whose files differ run to run cannot be diffed or deduplicated.
            with gzip.GzipFile(filename="", mode="wb", fileobj=tmp.open("wb"), mtime=0) as fh:
                fh.write(text.encode("utf-8"))
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return digest

    # --- reading ----------------------------------------------------------

    def get(self, digest: str) -> str:
        """The stored text. Raises when the hash is unknown."""
        path = self.path_for(digest)
        if not path.exists():
            raise SnapshotError(
                f"no snapshot {digest} in {self.root}. Extraction reads only from the "
                "store, so a missing snapshot means the fetch never completed."
            )
        return self._read(path)

    def has(self, digest: str) -> bool:
        return self.path_for(digest).exists()

    def verify(self, digest: str) -> bool:
        """Re-derive the hash from the stored bytes. False means corruption."""
        return content_hash(self.get(digest)) == digest

    def path_for(self, digest: str) -> Path:
        if not _HASH_RE.match(digest):
            raise SnapshotError(
                f"{digest!r} is not a sha256 hex digest; a snapshot is addressed by "
                "its content, never by a name someone chose"
            )
        return self.root / digest[:_SHARD_LEN] / f"{digest}{SUFFIX}"

    def uri(self, digest: str) -> str:
        """The value written to ``evidence.snapshot_uri``, stable across machines."""
        return f"snapshot://{digest}"

    # --- enumeration ------------------------------------------------------

    def iter_hashes(self) -> Iterator[str]:
        """Every stored hash, sorted. The evaluation harness replays from this."""
        if not self.root.exists():
            return
        for path in sorted(self.root.glob(f"*/*{SUFFIX}")):
            yield path.name[: -len(SUFFIX)]

    def stats(self) -> StoreStats:
        count = 0
        size = 0
        for path in self.root.glob(f"*/*{SUFFIX}"):
            count += 1
            size += path.stat().st_size
        return StoreStats(count=count, bytes_on_disk=size)

    @staticmethod
    def _read(path: Path) -> str:
        try:
            with gzip.open(path, "rb") as fh:
                return fh.read().decode("utf-8")
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise SnapshotError(f"snapshot {path.name} is unreadable: {exc}") from exc
