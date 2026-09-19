import gzip
import json

from wata.collect import append_snapshot, collect_one_snapshot


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def stop_departures(self, global_stop_ids):
        self.calls.append(global_stop_ids)
        return self.response


def test_collect_one_snapshot_tags_fetch_time_and_ids():
    client = FakeClient({"stops": []})
    record = collect_one_snapshot(client, ["A:1", "A:2"])
    assert record["global_stop_ids"] == ["A:1", "A:2"]
    assert record["response"] == {"stops": []}
    assert "fetched_at" in record
    assert client.calls == [["A:1", "A:2"]]


def test_append_snapshot_writes_gzipped_ndjson(tmp_path):
    path = tmp_path / "2026-09-18.ndjson.gz"
    append_snapshot({"a": 1}, path=path)
    append_snapshot({"b": 2}, path=path)

    with gzip.open(path, "rt", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f]
    assert lines == [{"a": 1}, {"b": 2}]
