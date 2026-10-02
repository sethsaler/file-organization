import io
import json

import pytest

import org_logging


@pytest.mark.parametrize("trailing_newline", [True, False])
def test_recent_history_handles_chunk_boundaries_and_bad_records(tmp_path, monkeypatch, trailing_newline):
    path = tmp_path / "history.jsonl"
    monkeypatch.setattr(org_logging, "default_history_path", lambda: path)
    records = [{"id": 1}, {"id": 2, "path": "照片/" * 5000}, {"id": 3}]
    lines = [json.dumps(record, ensure_ascii=False).encode() for record in records]
    path.write_bytes(b"\n".join([lines[0], lines[1], b"null", b"\xff", lines[2], b'{"partial":'])
                     + (b"\n" if trailing_newline else b""))
    assert org_logging.read_history(2) == records[:0:-1]
    assert org_logging.read_history(10) == records[::-1]
    assert org_logging.read_history(0) == []


def test_recent_history_only_reads_tail(monkeypatch):
    class CountingStream(io.BytesIO):
        bytes_read = 0

        def read(self, size=-1):
            result = super().read(size)
            self.bytes_read += len(result)
            return result

    stream = CountingStream(b'{"old":true}\n' * 100_000 + b'{"latest":true}\n')

    class HistoryPath:
        def open(self, mode):
            return stream

    monkeypatch.setattr(org_logging, "default_history_path", HistoryPath)
    assert org_logging.read_history(1) == [{"latest": True}]
    assert stream.bytes_read == 8192


def test_missing_history_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(org_logging, "default_history_path", lambda: tmp_path / "missing")
    assert org_logging.read_history() == []
