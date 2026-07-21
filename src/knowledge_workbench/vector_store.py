from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from .database import Database
from .embeddings import OllamaEmbeddingClient, normalize_rows
from .errors import KnowledgeWorkbenchError
from .utils import sha256_text, utc_now


class VectorStore(Protocol):
    def replace(self, ids: list[str], vectors: np.ndarray, metadata: dict) -> None: ...

    def search(self, vector: np.ndarray, limit: int) -> list[tuple[str, float]]: ...


@dataclass(frozen=True, slots=True)
class IndexBuildStats:
    evidence_count: int
    dimension: int
    embedding_seconds: float
    write_seconds: float
    total_seconds: float


class NumpyFlatVectorStore:
    """Simple exact cosine store, intentionally optimized for auditability first."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.vectors_path = directory / "evidence_vectors.npy"
        self.metadata_path = directory / "evidence_vectors.json"

    def replace(self, ids: list[str], vectors: np.ndarray, metadata: dict) -> None:
        if vectors.ndim != 2 or vectors.shape[0] != len(ids):
            raise KnowledgeWorkbenchError("向量矩阵与证据 ID 数量不一致")
        self.directory.mkdir(parents=True, exist_ok=True)
        vector_temp = self.vectors_path.with_suffix(".npy.tmp")
        metadata_temp = self.metadata_path.with_suffix(".json.tmp")
        with vector_temp.open("wb") as handle:
            np.save(handle, vectors.astype(np.float32, copy=False), allow_pickle=False)
        payload = {**metadata, "ids": ids}
        metadata_temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        vector_temp.replace(self.vectors_path)
        metadata_temp.replace(self.metadata_path)

    def search(self, vector: np.ndarray, limit: int) -> list[tuple[str, float]]:
        metadata = self.metadata()
        vectors = np.load(self.vectors_path, allow_pickle=False, mmap_mode="r")
        query = np.asarray(vector, dtype=np.float32).reshape(1, -1)
        query = normalize_rows(query)[0]
        if vectors.ndim != 2 or vectors.shape[1] != query.shape[0]:
            raise KnowledgeWorkbenchError(
                f"查询向量维度 {query.shape[0]} 与索引维度不一致"
            )
        scores = np.asarray(vectors @ query)
        count = min(max(limit, 0), len(scores))
        if count == 0:
            return []
        candidates = np.argpartition(scores, -count)[-count:]
        ranked = candidates[np.argsort(scores[candidates])[::-1]]
        ids = metadata["ids"]
        return [(ids[int(index)], float(scores[index])) for index in ranked]

    def metadata(self) -> dict:
        if not self.vectors_path.is_file() or not self.metadata_path.is_file():
            raise KnowledgeWorkbenchError("向量索引不存在，请先运行 index build")
        return json.loads(self.metadata_path.read_text(encoding="utf-8"))


def build_evidence_index(
    database: Database,
    store: NumpyFlatVectorStore,
    client: OllamaEmbeddingClient,
    *,
    batch_size: int = 16,
) -> IndexBuildStats:
    started = time.perf_counter()
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT id, excerpt, updated_at
            FROM evidence
            WHERE status NOT IN ('archived', 'deprecated')
            ORDER BY id
            """
        ).fetchall()
    if not rows:
        raise KnowledgeWorkbenchError("没有可索引的原子证据")

    ids = [row["id"] for row in rows]
    texts = [row["excerpt"] for row in rows]
    embedding_started = time.perf_counter()
    batches = [
        client.embed(texts[index : index + batch_size])
        for index in range(0, len(texts), batch_size)
    ]
    vectors = normalize_rows(np.vstack(batches))
    embedding_seconds = time.perf_counter() - embedding_started

    fingerprint = sha256_text(
        "\n".join(f"{row['id']}:{row['updated_at']}" for row in rows)
    )
    write_started = time.perf_counter()
    store.replace(
        ids,
        vectors,
        {
            "format_version": 1,
            "model": client.model,
            "dimension": int(vectors.shape[1]),
            "count": len(ids),
            "built_at": utc_now(),
            "evidence_fingerprint": fingerprint,
        },
    )
    write_seconds = time.perf_counter() - write_started
    return IndexBuildStats(
        evidence_count=len(ids),
        dimension=int(vectors.shape[1]),
        embedding_seconds=embedding_seconds,
        write_seconds=write_seconds,
        total_seconds=time.perf_counter() - started,
    )


def semantic_search(
    database: Database,
    store: NumpyFlatVectorStore,
    client: OllamaEmbeddingClient,
    query: str,
    limit: int,
):
    query_vector = client.embed([query])[0]
    ranked = store.search(query_vector, limit)
    if not ranked:
        return []
    rank_by_id = {evidence_id: (rank, score) for rank, (evidence_id, score) in enumerate(ranked)}
    placeholders = ",".join("?" for _ in ranked)
    with database.connect() as connection:
        rows = connection.execute(
            f"""
            SELECT e.id, e.status, e.excerpt, e.locator_json,
                   d.original_name, d.classification
            FROM evidence e
            JOIN document_versions dv ON dv.id = e.document_version_id
            JOIN documents d ON d.id = dv.document_id
            WHERE e.id IN ({placeholders})
            """,
            tuple(evidence_id for evidence_id, _ in ranked),
        ).fetchall()
    return sorted(
        ((row, rank_by_id[row["id"]][1]) for row in rows),
        key=lambda item: rank_by_id[item[0]["id"]][0],
    )

