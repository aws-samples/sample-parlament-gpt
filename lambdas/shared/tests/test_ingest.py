"""Tests for the batch-ingest document model and store.

These back the three jurisdictions with no queryable debates API (France, Netherlands, Australia),
where we must ingest into our own index and answer from it.
"""
import json

import pytest

from gov_debates.contracts import validate
from gov_debates.ingest.documents import (
    IndexedSpeech,
    build_search_key,
    fold,
    matches,
    tokenize,
)
from gov_debates.ingest.store import (
    InMemorySpeechStore,
    S3SpeechStore,
    _month_range,
    store_from_env,
)


def _speech(**overrides) -> IndexedSpeech:
    base = dict(
        jurisdiction="fr",
        jurisdiction_label="Assemblée nationale",
        doc_id="fr:2026-06-11:CRS123",
        source_url="https://www.assemblee-nationale.fr/dyn/17/comptes-rendus/seance/x",
        title="Débat sur les retraites",
        date="2026-06-11",
        speaker="Élisabeth Borne",
        group="Renaissance",
        full_text="Monsieur le Président, la réforme des retraites est nécessaire.",
    )
    base.update(overrides)
    return IndexedSpeech(**base)


class TestDocuments:
    def test_search_key_is_derived_on_construction(self):
        s = _speech()
        assert s.search_key
        assert "retraites" in s.search_key

    def test_fold_strips_accents_and_case(self):
        # A user typing "elisabeth borne" must find "Élisabeth Borne".
        assert fold("Élisabeth BORNE") == "elisabeth borne"
        assert fold("énergie") == "energie"
        assert fold(None) == ""

    def test_matches_uses_and_semantics(self):
        s = _speech()
        assert matches(s, ["retraites"])
        assert matches(s, ["reforme", "retraites"])       # accent-insensitive
        assert not matches(s, ["retraites", "climat"])    # AND, not OR

    def test_tokenize_honours_quoted_phrases(self):
        assert tokenize('"reforme des retraites" climat') == ["reforme des retraites", "climat"]
        assert tokenize("climat energie") == ["climat", "energie"]
        assert tokenize(None) == []

    def test_projection_onto_the_wire_contract_validates(self):
        row = _speech().to_speech_result().to_dict()
        validate(row)
        assert row["jurisdiction"] == "fr"
        assert row["speaker"] == "Élisabeth Borne"
        # full_text and search_key are index-only and must not leak onto the wire.
        assert "full_text" not in row
        assert "search_key" not in row

    def test_projection_centres_the_snippet_on_the_query(self):
        long_text = ("bla " * 500) + "CLIMAT URGENT " + ("bla " * 500)
        s = _speech(full_text=long_text)
        snippet = s.to_speech_result(snippet_query="climat").snippet
        assert "CLIMAT URGENT" in snippet

    def test_roundtrip_through_dict(self):
        s = _speech()
        again = IndexedSpeech.from_dict(json.loads(json.dumps(s.to_dict())))
        assert again.doc_id == s.doc_id
        assert again.full_text == s.full_text

    def test_from_dict_ignores_unknown_keys(self):
        data = _speech().to_dict()
        data["unexpected"] = "value"
        assert IndexedSpeech.from_dict(data).doc_id == "fr:2026-06-11:CRS123"


class TestInMemoryStore:
    def _store(self):
        # NOTE: the search key spans title + speaker + body, so titles are varied here
        # deliberately — a shared title would make every row match every title term.
        return InMemorySpeechStore([
            _speech(doc_id="a", date="2026-06-11", title="Débat sur les retraites",
                    full_text="retraites et climat"),
            _speech(doc_id="b", date="2026-05-02", title="Débat sur l'énergie",
                    full_text="climat seulement", speaker="Jean Dupont"),
            _speech(doc_id="c", date="2025-01-15", title="Autre débat", full_text="autre sujet"),
            _speech(doc_id="d", jurisdiction="nl", jurisdiction_label="Tweede Kamer",
                    date="2026-06-01", title="Klimaatdebat", full_text="klimaat"),
        ])

    def test_query_filters_by_jurisdiction(self):
        rows, total = self._store().query(jurisdiction="nl", terms=[], limit=10)
        assert total == 1 and rows[0].doc_id == "d"

    def test_query_free_text_and_semantics(self):
        rows, total = self._store().query(jurisdiction="fr", terms=["climat"], limit=10)
        assert {r.doc_id for r in rows} == {"a", "b"}
        rows, _ = self._store().query(jurisdiction="fr", terms=["climat", "retraites"], limit=10)
        assert {r.doc_id for r in rows} == {"a"}

    def test_query_date_range(self):
        rows, _ = self._store().query(
            jurisdiction="fr", terms=[], date_start="2026-01-01", date_end="2026-12-31", limit=10
        )
        assert {r.doc_id for r in rows} == {"a", "b"}

    def test_query_speaker_filter_is_accent_insensitive(self):
        rows, _ = self._store().query(jurisdiction="fr", terms=[], speaker="elisabeth borne", limit=10)
        assert {r.doc_id for r in rows} == {"a", "c"}

    def test_results_are_newest_first(self):
        rows, _ = self._store().query(jurisdiction="fr", terms=[], limit=10)
        assert [r.date for r in rows] == sorted([r.date for r in rows], reverse=True)

    def test_pagination_via_offset(self):
        page1, total = self._store().query(jurisdiction="fr", terms=[], offset=0, limit=2)
        page2, _ = self._store().query(jurisdiction="fr", terms=[], offset=2, limit=2)
        assert total == 3
        assert {r.doc_id for r in page1}.isdisjoint({r.doc_id for r in page2})

    def test_get_by_id(self):
        assert self._store().get(jurisdiction="fr", doc_id="a").doc_id == "a"
        assert self._store().get(jurisdiction="fr", doc_id="missing") is None
        # A doc_id from another jurisdiction must not leak across.
        assert self._store().get(jurisdiction="fr", doc_id="d") is None


class FakeS3:
    def __init__(self, objects=None):
        self.objects = objects or {}
        self.puts = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": _Body(self.objects[Key])}

    def list_objects_v2(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        keys = [k for k in self.objects if k.startswith(prefix)]
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body.decode("utf-8")
        self.puts.append(Key)
        return {}


class _Body:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text.encode("utf-8")


class TestS3Store:
    def _jsonl(self, *speeches):
        return "\n".join(json.dumps(s.to_dict(), ensure_ascii=False) for s in speeches) + "\n"

    def test_reads_month_partitioned_shards(self):
        s3 = FakeS3({
            "speeches/fr/2026-06.jsonl": self._jsonl(
                _speech(doc_id="fr:2026-06-11:a", date="2026-06-11", full_text="climat"),
            ),
            "speeches/fr/2026-05.jsonl": self._jsonl(
                _speech(doc_id="fr:2026-05-02:b", date="2026-05-02", full_text="retraites"),
            ),
        })
        store = S3SpeechStore("bucket", "speeches", client=s3)
        rows, total = store.query(
            jurisdiction="fr", terms=["climat"], date_start="2026-05-01", date_end="2026-06-30",
            limit=10,
        )
        assert total == 1 and rows[0].doc_id == "fr:2026-06-11:a"

    def test_missing_shard_is_treated_as_no_sittings(self):
        store = S3SpeechStore("bucket", "speeches", client=FakeS3({}))
        rows, total = store.query(
            jurisdiction="fr", terms=[], date_start="2026-01-01", date_end="2026-03-31", limit=10
        )
        assert rows == [] and total == 0

    def test_corrupt_line_is_skipped_not_fatal(self):
        s3 = FakeS3({"speeches/fr/2026-06.jsonl": "{not json\n" + self._jsonl(_speech(doc_id="ok"))})
        store = S3SpeechStore("bucket", "speeches", client=s3)
        rows, total = store.query(
            jurisdiction="fr", terms=[], date_start="2026-06-01", date_end="2026-06-30", limit=10
        )
        assert total == 1 and rows[0].doc_id == "ok"

    def test_get_uses_the_month_embedded_in_the_doc_id(self):
        s3 = FakeS3({
            "speeches/fr/2026-06.jsonl": self._jsonl(_speech(doc_id="fr:2026-06-11:a", date="2026-06-11")),
        })
        store = S3SpeechStore("bucket", "speeches", client=s3)
        assert store.get(jurisdiction="fr", doc_id="fr:2026-06-11:a") is not None

    def test_put_shard_writes_ndjson_and_busts_the_cache(self):
        s3 = FakeS3({})
        store = S3SpeechStore("bucket", "speeches", client=s3)
        written = store.put_shard("fr", "2026-06", [_speech(doc_id="x", date="2026-06-11")])
        assert written == 1
        assert "speeches/fr/2026-06.jsonl" in s3.puts
        rows, total = store.query(
            jurisdiction="fr", terms=[], date_start="2026-06-01", date_end="2026-06-30", limit=10
        )
        assert total == 1

    def test_requires_a_bucket(self):
        with pytest.raises(ValueError):
            S3SpeechStore("", "speeches", client=FakeS3({}))


def test_month_range_is_newest_first_and_capped():
    months = _month_range("2026-01-01", "2026-06-30", cap=24)
    assert months[0] == "2026-06" and months[-1] == "2026-01"
    assert len(_month_range("2000-01-01", "2026-12-31", cap=3)) == 3
    assert _month_range("garbage", "2026-01-01", cap=5) == []


def test_store_from_env_falls_back_to_memory(monkeypatch):
    monkeypatch.delenv("INDEX_BUCKET", raising=False)
    assert isinstance(store_from_env(), InMemorySpeechStore)
