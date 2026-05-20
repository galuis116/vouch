"""Embedding storage layer -- schema, put, get, search."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from vouch import index_db
from vouch.embeddings.base import content_hash
from vouch.storage import KBStore


@pytest.fixture
def kb_dir(tmp_path: Path) -> Path:
    store = KBStore.init(tmp_path)
    return store.kb_dir


def test_embedding_schema_creates_tables(kb_dir: Path) -> None:
    with index_db.open_db(kb_dir) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','virtual table')"
        )}
    assert "embedding_index" in tables
    assert "query_embedding_cache" in tables
    assert "embedding_dupes" in tables


def test_embedding_meta_default_values(kb_dir: Path) -> None:
    meta = index_db.get_embedding_meta(kb_dir)
    assert meta.get("embedding_model") in (None, "")


def test_put_and_get_embedding(kb_dir: Path) -> None:
    vec = np.zeros(8, dtype=np.float32)
    vec[0] = 1.0
    h = content_hash("hello")
    with index_db.open_db(kb_dir) as conn:
        index_db.put_embedding(
            conn, kind="claim", id="c1", vec=vec,
            content_hash=h, model="mock", model_version="1", dim=8,
        )
    got = index_db.get_embedding(kb_dir, kind="claim", id="c1")
    assert got is not None
    rec_vec, rec_hash, rec_model = got
    assert np.allclose(rec_vec, vec)
    assert rec_hash == h
    assert rec_model == "mock"


def test_put_embedding_idempotent_on_same_hash(kb_dir: Path) -> None:
    vec = np.ones(4, dtype=np.float32)
    h = content_hash("same")
    with index_db.open_db(kb_dir) as conn:
        index_db.put_embedding(
            conn, kind="claim", id="c1", vec=vec, content_hash=h,
            model="mock", model_version="1", dim=4,
        )
        index_db.put_embedding(
            conn, kind="claim", id="c1", vec=vec, content_hash=h,
            model="mock", model_version="1", dim=4,
        )
    with index_db.open_db(kb_dir) as conn:
        n = conn.execute("SELECT COUNT(*) FROM embedding_index WHERE id='c1'").fetchone()[0]
    assert n == 1


def test_set_embedding_meta_round_trip(kb_dir: Path) -> None:
    index_db.set_embedding_meta(
        kb_dir,
        model="sentence-transformers/all-mpnet-base-v2",
        version="v1",
        dim=768,
    )
    meta = index_db.get_embedding_meta(kb_dir)
    assert meta["embedding_model"] == "sentence-transformers/all-mpnet-base-v2"
    assert meta["embedding_dim"] == "768"
