from __future__ import annotations

import math
import statistics
import time

from .database import Database
from .errors import KnowledgeWorkbenchError
from .search import search_evidence_with_mode
from .utils import sha256_text, utc_now


def benchmark_fts_search(
    database: Database,
    queries: list[str],
    *,
    iterations: int = 20,
    limit: int = 30,
) -> dict:
    normalized_queries = [query.strip() for query in queries if query.strip()]
    if not normalized_queries:
        raise KnowledgeWorkbenchError("全文检索性能测试至少需要一个非空查询")
    if iterations < 1:
        raise KnowledgeWorkbenchError("iterations 必须大于 0")
    if limit < 1 or limit > 200:
        raise KnowledgeWorkbenchError("limit 必须在 1 到 200 之间")

    with database.connect() as connection:
        corpus = connection.execute(
            """
            SELECT COUNT(DISTINCT d.id) AS document_count,
                   COUNT(DISTINCT e.id) AS evidence_count
            FROM evidence e
            JOIN processing_runs pr
              ON pr.id = e.processing_run_id AND pr.is_current = 1
            JOIN document_versions dv ON dv.id = e.document_version_id
            JOIN documents d
              ON d.id = dv.document_id AND d.current_version_id = dv.id
            """
        ).fetchone()

    cases = []
    all_latencies = []
    for query in normalized_queries:
        search_evidence_with_mode(database, query, limit)
        latencies = []
        modes: dict[str, int] = {}
        result_count = 0
        for _ in range(iterations):
            started = time.perf_counter()
            rows, mode = search_evidence_with_mode(database, query, limit)
            latencies.append((time.perf_counter() - started) * 1000)
            modes[mode] = modes.get(mode, 0) + 1
            result_count = len(rows)
        all_latencies.extend(latencies)
        cases.append(
            {
                "query_sha256": sha256_text(query),
                "result_count": result_count,
                "mode_counts": modes,
                "median_ms": round(statistics.median(latencies), 6),
                "p95_ms": round(_percentile(latencies, 0.95), 6),
                "max_ms": round(max(latencies), 6),
            }
        )

    return {
        "schema_version": "1.0",
        "kind": "fts-search-benchmark",
        "measured_at": utc_now(),
        "corpus": {
            "document_count": corpus["document_count"],
            "current_evidence_count": corpus["evidence_count"],
        },
        "configuration": {
            "query_count": len(normalized_queries),
            "iterations_per_query": iterations,
            "limit": limit,
            "warmup_per_query": 1,
        },
        "aggregate": {
            "measurement_count": len(all_latencies),
            "median_ms": round(statistics.median(all_latencies), 6),
            "p95_ms": round(_percentile(all_latencies, 0.95), 6),
            "max_ms": round(max(all_latencies), 6),
            "fts5_case_count": sum("fts5" in case["mode_counts"] for case in cases),
            "substring_fallback_case_count": sum(
                "substring_fallback" in case["mode_counts"] for case in cases
            ),
        },
        "cases": cases,
    }


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]
