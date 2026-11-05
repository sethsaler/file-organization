"""Persistent duplicate lookups must remain selective and validate keepers."""
import os

import org_dupes
from org_dupes import PersistentDuplicateIndex, SQLiteHashCache


def test_reuses_index_without_reading_unrelated_files(tmp_path, monkeypatch):
    db = tmp_path / "index.sqlite3"
    cache = SQLiteHashCache(db)
    index = PersistentDuplicateIndex(cache, "scope")
    paths = []
    for i in range(200):
        path = tmp_path / f"file-{i}.jpg"
        path.write_bytes(f"{i:08d}".encode())
        paths.append(path)
        index.register(path, 8)
    index.finish()
    cache.close()
    calls = []
    real = org_dupes.HashCache.fingerprint
    monkeypatch.setattr(SQLiteHashCache, "fingerprint", staticmethod(lambda p: calls.append(p) or real(p)))
    cache = SQLiteHashCache(db)
    index = PersistentDuplicateIndex(cache, "scope", incremental=True)
    assert index.reused
    incoming = tmp_path / "incoming.jpg"
    incoming.write_bytes(paths[42].read_bytes())
    assert index.register(incoming, 8) == paths[42]
    index.finish()
    assert len(set(calls)) == 2  # incoming and the potential keeper only
    cache.close()


def test_stale_keeper_is_never_returned(tmp_path):
    db = tmp_path / "index.sqlite3"
    original = tmp_path / "original"
    other = tmp_path / "other"
    original.write_bytes(b"same")
    other.write_bytes(b"diff")
    cache = SQLiteHashCache(db)
    index = PersistentDuplicateIndex(cache, "scope")
    index.register(original, 4)
    index.register(other, 4)
    index.finish()
    cache.close()
    mtime = original.stat().st_mtime_ns
    original.write_bytes(b"edit")
    os.utime(original, ns=(mtime, mtime))
    incoming = tmp_path / "incoming"
    incoming.write_bytes(b"same")
    cache = SQLiteHashCache(db)
    index = PersistentDuplicateIndex(cache, "scope", incremental=True)
    assert index.register(incoming, 4) is None
    incoming2 = tmp_path / "incoming2"
    incoming2.write_bytes(b"same")
    assert index.register(incoming2, 4) == incoming
    cache.close()


def test_changed_paths_and_scope_require_reconciliation(tmp_path):
    db = tmp_path / "index.sqlite3"
    original = tmp_path / "original"
    original.write_bytes(b"same")
    cache = SQLiteHashCache(db)
    index = PersistentDuplicateIndex(cache, "scope")
    index.register(original, 4)
    index.finish()
    cache.close()
    cache = SQLiteHashCache(db)
    index = PersistentDuplicateIndex(cache, "scope", incremental=True, changed_paths=[original])
    assert index.reused
    assert cache.connection.execute("SELECT count(*) FROM canonical").fetchone()[0] == 0
    cache.close()
    cache = SQLiteHashCache(db)
    assert not PersistentDuplicateIndex(cache, "new routing", incremental=True).reused
    cache.close()


def test_incomplete_index_transaction_is_not_published(tmp_path):
    db = tmp_path / "index.sqlite3"
    cache = SQLiteHashCache(db)
    PersistentDuplicateIndex(cache, "scope")
    cache.close()
    cache = SQLiteHashCache(db)
    assert not PersistentDuplicateIndex(cache, "scope", incremental=True).reused
    cache.close()


def test_read_only_cache_never_creates_or_modifies_database(tmp_path):
    db = tmp_path / "index.sqlite3"
    path = tmp_path / "file"
    path.write_bytes(b"content")
    cache = SQLiteHashCache(db, read_only=True)
    digest = cache.get(path)
    cache.save()
    assert not db.exists()
    cache.close()
    cache = SQLiteHashCache(db)
    assert cache.get(path) == digest
    cache.save()
    cache.close()
    before = db.read_bytes()
    cache = SQLiteHashCache(db, read_only=True)
    assert cache.get(path) == digest
    assert cache.hits == 1
    cache.save()
    cache.close()
    assert db.read_bytes() == before


def test_corrupt_database_falls_back_to_safe_hashing(tmp_path):
    db = tmp_path / "index.sqlite3"
    db.write_bytes(b"broken")
    cache = SQLiteHashCache(db)
    index = PersistentDuplicateIndex(cache, "scope", incremental=True)
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"same")
    b.write_bytes(b"same")
    assert not index.reused
    assert index.register(a, 4) is None
    assert index.register(b, 4) == a
    index.finish()
    cache.close()


def test_moved_canonical_is_persisted(tmp_path):
    db = tmp_path / "index.sqlite3"
    a, b, incoming = tmp_path / "a", tmp_path / "b", tmp_path / "incoming"
    a.write_bytes(b"same")
    cache = SQLiteHashCache(db)
    index = PersistentDuplicateIndex(cache, "scope")
    index.register(a, 4)
    a.rename(b)
    index.update_location(4, a, b)
    index.finish()
    cache.close()
    incoming.write_bytes(b"same")
    cache = SQLiteHashCache(db)
    index = PersistentDuplicateIndex(cache, "scope", incremental=True)
    assert index.register(incoming, 4) == b
    cache.close()
