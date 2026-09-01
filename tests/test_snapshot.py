"""E2.S2 — the content-addressed snapshot store.

The store is the audit trail. The properties worth testing are the ones an
auditor would rely on: the same page stores once, stored bytes never change, a
round trip is exact, and a missing snapshot is a loud error rather than an empty
string that reads like a page with nothing on it.
"""

from __future__ import annotations

import gzip
import os
from pathlib import Path

import pytest

from finder.acquire.snapshot import (
    SUFFIX,
    SnapshotError,
    SnapshotStore,
    content_hash,
    normalize,
)

PAGE = """# Call for Speakers

AI Week 2026 is accepting proposals from speakers, trainers and sponsors.

Tracks: manufacturing, healthcare, financial services, workforce.
Contact: zack@joineta.org
"""


@pytest.fixture
def store(tmp_path: Path) -> SnapshotStore:
    return SnapshotStore(tmp_path / "snapshots")


# --- identity --------------------------------------------------------------


def test_the_same_page_hashes_the_same() -> None:
    assert content_hash(PAGE) == content_hash(PAGE)
    assert len(content_hash(PAGE)) == 64


def test_reflowed_whitespace_is_the_same_page() -> None:
    """Providers re-wrap markdown between calls. Treating a re-wrap as new
    content would store the same page a dozen times and defeat the cache."""
    reflowed = PAGE.replace("\n", "\n\n").replace("Tracks:", "Tracks:   ")
    assert content_hash(reflowed) == content_hash(PAGE)


def test_changed_words_are_a_different_page() -> None:
    changed = PAGE.replace("manufacturing", "hospitality")
    assert content_hash(changed) != content_hash(PAGE)


def test_normalize_collapses_and_trims() -> None:
    assert normalize("  a \n\n b\t c  ") == "a b c"


# --- round trip ------------------------------------------------------------


def test_put_then_get_returns_the_bytes_exactly(store: SnapshotStore) -> None:
    """Normalisation is for identity only. What comes back is what went in —
    a span check against a re-wrapped copy would produce false fabrications."""
    digest = store.put(PAGE)
    assert store.get(digest) == PAGE
    assert store.has(digest)
    assert store.verify(digest)


def test_unicode_survives_the_round_trip(store: SnapshotStore) -> None:
    text = "Präsentation — “quoted” · 日本語 · naïve café\n"
    assert store.get(store.put(text)) == text


def test_a_very_large_page_round_trips(store: SnapshotStore) -> None:
    text = "lorem ipsum dolor sit amet\n" * 50_000
    digest = store.put(text)
    assert store.get(digest) == text
    stored = store.path_for(digest).stat().st_size
    assert stored < len(text.encode()) / 10, "gzip is not actually compressing"


# --- write-once ------------------------------------------------------------


def test_putting_the_same_page_twice_writes_once(store: SnapshotStore) -> None:
    """Proven on bytes, not on mtime: Windows timestamp granularity is coarse
    enough that a rewrite inside one tick would look like no write at all."""
    digest = store.put(PAGE)
    path = store.path_for(digest)

    # Same text, deliberately different archive bytes. A rewrite would replace
    # these with the store's own canonical encoding.
    with gzip.GzipFile(filename="", mode="wb", fileobj=path.open("wb"), mtime=99) as fh:
        fh.write(PAGE.encode("utf-8"))
    marked = path.read_bytes()

    assert store.put(PAGE) == digest
    assert path.read_bytes() == marked, "the file was rewritten"
    assert store.stats().count == 1


def test_a_reflowed_copy_does_not_create_a_second_file(store: SnapshotStore) -> None:
    store.put(PAGE)
    store.put(PAGE.replace("\n", "\n\n"))
    assert store.stats().count == 1


def test_different_content_under_an_existing_hash_is_refused(store: SnapshotStore) -> None:
    """A silent overwrite would rewrite history, and history is the only thing
    this class is for."""
    digest = store.put(PAGE)
    path = store.path_for(digest)
    with gzip.open(path, "wb") as fh:
        fh.write(b"something else entirely")

    with pytest.raises(SnapshotError, match="refusing to overwrite"):
        store.put(PAGE)


def test_the_store_has_no_delete(store: SnapshotStore) -> None:
    """Structural, not conventional: an audit trail must not depend on someone
    remembering not to delete from it."""
    for name in ("delete", "remove", "purge", "clear", "pop"):
        assert not hasattr(store, name), f"SnapshotStore grew a {name}() method"


def test_stored_bytes_are_deterministic(tmp_path: Path) -> None:
    """Same text, same file. A store whose bytes differ run to run cannot be
    diffed, deduplicated or synced."""
    a, b = SnapshotStore(tmp_path / "a"), SnapshotStore(tmp_path / "b")
    digest = a.put(PAGE)
    assert b.put(PAGE) == digest
    assert a.path_for(digest).read_bytes() == b.path_for(digest).read_bytes()


# --- failure modes ---------------------------------------------------------


def test_a_missing_snapshot_raises_rather_than_returning_nothing(store: SnapshotStore) -> None:
    """An empty string would read like a page that said nothing, and the
    extractor would faithfully record not_stated for a page it never saw."""
    missing = "0" * 64
    assert not store.has(missing)
    with pytest.raises(SnapshotError, match="no snapshot"):
        store.get(missing)


def test_a_non_hash_key_is_refused(store: SnapshotStore) -> None:
    """Addressed by content, never by a name someone chose."""
    for bad in ("../../etc/passwd", "latest", "ABCDEF", "", "0" * 63, "0" * 65):
        with pytest.raises(SnapshotError, match="not a sha256"):
            store.path_for(bad)


def test_a_corrupt_archive_is_a_clear_error_not_a_traceback(store: SnapshotStore) -> None:
    digest = store.put(PAGE)
    store.path_for(digest).write_bytes(b"not gzip at all")
    with pytest.raises(SnapshotError, match="unreadable"):
        store.get(digest)


def test_verify_detects_content_that_no_longer_matches_its_hash(store: SnapshotStore) -> None:
    """The check an audit actually needs: do these bytes still hash to this name?"""
    digest = store.put(PAGE)
    with gzip.open(store.path_for(digest), "wb") as fh:
        fh.write(b"tampered")
    assert store.verify(digest) is False


def test_no_temporary_files_are_left_behind(store: SnapshotStore) -> None:
    store.put(PAGE)
    assert not list(store.root.rglob("*.tmp"))


# --- layout and enumeration ------------------------------------------------


def test_snapshots_are_sharded_by_hash_prefix(store: SnapshotStore) -> None:
    """Snapshots are retained forever; a flat directory reaches five figures
    within months and degrades badly on NTFS."""
    digest = store.put(PAGE)
    path = store.path_for(digest)
    assert path.parent.name == digest[:2]
    assert path.name == f"{digest}{SUFFIX}"
    assert path.parent.parent == store.root


def test_iter_hashes_lists_everything_stored(store: SnapshotStore) -> None:
    digests = {store.put(f"page number {i}") for i in range(12)}
    assert set(store.iter_hashes()) == digests
    assert list(store.iter_hashes()) == sorted(store.iter_hashes())


def test_an_empty_store_enumerates_to_nothing(tmp_path: Path) -> None:
    empty = SnapshotStore(tmp_path / "never-written")
    assert list(empty.iter_hashes()) == []
    assert empty.stats().count == 0


def test_stats_report_what_is_on_disk(store: SnapshotStore) -> None:
    for i in range(3):
        store.put(f"page {i}")
    stats = store.stats()
    assert stats.count == 3
    assert stats.bytes_on_disk > 0


def test_the_uri_is_stable_across_machines(store: SnapshotStore) -> None:
    """evidence.snapshot_uri must not embed a local path, or the audit trail
    stops resolving the moment the data directory moves."""
    digest = store.put(PAGE)
    uri = store.uri(digest)
    assert uri == f"snapshot://{digest}"
    assert os.sep not in uri
    assert str(store.root) not in uri
