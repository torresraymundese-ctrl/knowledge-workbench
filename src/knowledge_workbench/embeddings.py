from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

import numpy as np

from .errors import KnowledgeWorkbenchError
from .policy import enforce_model_selection


@dataclass(slots=True)
class OllamaEmbeddingClient:
    model: str = "bge-m3"
    base_url: str = "http://127.0.0.1:11434"
    timeout_seconds: float = 120.0

    def embed(self, texts: list[str]) -> np.ndarray:
        enforce_model_selection(self.model)
        if not texts:
            return np.empty((0, 0), dtype=np.float32)
        payload = json.dumps(
            {"model": self.model, "input": texts}, ensure_ascii=False
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise KnowledgeWorkbenchError(
                f"无法调用本地 Ollama 嵌入接口（{self.base_url}）：{exc}"
            ) from exc
        embeddings = data.get("embeddings")
        if not embeddings:
            raise KnowledgeWorkbenchError(
                f"Ollama 模型 {self.model} 未返回向量，请确认模型已安装"
            )
        vectors = np.asarray(embeddings, dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[0] != len(texts):
            raise KnowledgeWorkbenchError("Ollama 返回的向量数量或维度不正确")
        return vectors


def normalize_rows(vectors: np.ndarray) -> np.ndarray:
    if vectors.size == 0:
        return vectors.astype(np.float32, copy=False)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vectors / norms).astype(np.float32, copy=False)
