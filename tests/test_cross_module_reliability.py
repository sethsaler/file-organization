"""Race regressions spanning move publication, recovery, and duplicate lookup."""
import errno
import os

import pytest

import org_paths
from org_dupes import PersistentDuplicateIndex, SQLiteHashCache
from org_manifest import RecoveryJournal


def test_cross_volume_source_changed_during_journal_flush_is_retained(tmp_path, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"old")
    original = org_paths._rename_exclusive

    def cross_volume(src, dst):
        if src == source:
            raise OSError(errno.EXDEV, "different volume")
        return original(src, dst)

    monkeypatch.setattr(org_paths, "_rename_exclusive", cross_volume)
    with pytest.raises((OSError, ValueError)):
        org_paths.move_no_replace(source, destination,
                                  after_publish=lambda path: source.write_bytes(b"new bytes"))
    assert source.read_bytes() == b"new bytes"
    assert destination.read_bytes() == b"old"


@pytest.mark.parametrize("changed", ["source", "canonical"])
def test_hardlink_changed_during_journal_flush_preserves_source(tmp_path, changed):
    source, canonical, destination = tmp_path / "source", tmp_path / "canonical", tmp_path / "destination"
    source.write_bytes(b"same")
    canonical.write_bytes(b"same")
    journal = RecoveryJournal(tmp_path)
    append = journal._append

    def change_after_done(event):
        append(event)
        if event["event"] == "done":
            (source if changed == "source" else canonical).write_bytes(b"new bytes")

    journal._append = change_after_done
    try:
        with pytest.raises((OSError, ValueError)):
            journal.hardlink_move(source, canonical, destination)
        assert source.read_bytes() == (b"new bytes" if changed == "source" else b"same")
    finally:
        journal.finish(status="failed")


@pytest.mark.parametrize("changed", ["incoming", "keeper"])
def test_duplicate_lookup_rejects_post_hash_content_change(tmp_path, changed):
    keeper, incoming = tmp_path / "keeper", tmp_path / "incoming"
    keeper.write_bytes(b"same")
    incoming.write_bytes(b"same")
    cache = SQLiteHashCache(tmp_path / "index.sqlite3")
    index = PersistentDuplicateIndex(cache, "scope")
    index.register(keeper, 4)
    original = cache.get

    def change_after_hash(path):
        digest = original(path)
        if path == (incoming if changed == "incoming" else keeper):
            stamp = path.stat().st_mtime_ns
            path.write_bytes(b"edit")
            os.utime(path, ns=(stamp, stamp))
        return digest

    cache.get = change_after_hash
    try:
        assert index.register(incoming, 4) is None
        assert keeper.read_bytes() != incoming.read_bytes()
    finally:
        cache.close()
