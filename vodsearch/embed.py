"""
vodsearch/embed.py — semantic search over transcript lines, fully local.

Keyword search (FTS5) needs the words to match; "that time Loki wrecked
me" finds nothing when James said "this Loki is disgusting". Embeddings
fix that: every transcript line becomes a 768-dim vector from a local
Ollama model (nomic-embed-text, ~270 MB, runs on the GPU in
milliseconds), a query becomes one too, and cosine similarity ranks the
lines. Nothing leaves the PC.

Vectors are stored unit-normalized in the `embeddings` table (one row
per segment), so similarity is a single matrix-vector dot product in
numpy: 13k lines x 768 floats is 40 MB and ranks in a few milliseconds.

nomic-embed-text is asymmetric: documents are embedded with the
"search_document: " prefix and queries with "search_query: ".
"""

from __future__ import annotations

import json
import urllib.request
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np

DEFAULT_MODEL = "nomic-embed-text"
DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


def normalize(mat: np.ndarray) -> np.ndarray:
    mat = np.asarray(mat, dtype=np.float32)
    if mat.ndim == 1:
        mat = mat[None, :]
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


class OllamaEmbedder:
    """Batches texts through Ollama's /api/embed. Synchronous (the
    indexer is a script; the web layer calls it in a thread)."""

    name = "ollama"

    def __init__(self, host: str = "http://localhost:11434", model: str = DEFAULT_MODEL,
                 batch_size: int = 64, timeout_s: float = 120.0):
        self.host = host.rstrip("/")
        self.model = model
        self.batch_size = max(1, int(batch_size))
        self.timeout_s = timeout_s

    def _post(self, texts: Sequence[str]) -> np.ndarray:
        body = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        req = urllib.request.Request(self.host + "/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            data = json.loads(r.read().decode("utf-8"))
        embs = data.get("embeddings") or []
        if len(embs) != len(texts):
            raise RuntimeError(f"ollama returned {len(embs)} embeddings for {len(texts)} inputs")
        return normalize(np.asarray(embs, dtype=np.float32))

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        out: List[np.ndarray] = []
        for i in range(0, len(texts), self.batch_size):
            chunk = [DOC_PREFIX + (t or "") for t in texts[i:i + self.batch_size]]
            out.append(self._post(chunk))
        return np.concatenate(out) if out else np.zeros((0, 0), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return self._post([QUERY_PREFIX + (text or "")])[0]

    def available(self) -> bool:
        try:
            self._post(["ping"])
            return True
        except Exception:
            return False


def top_k(query_vec: np.ndarray, ids: np.ndarray, matrix: np.ndarray, k: int = 20,
          min_score: float = 0.0) -> List[Tuple[int, float]]:
    """(segment_id, cosine) pairs, best first, for unit-normalized inputs."""
    if matrix.size == 0 or ids.size == 0:
        return []
    q = normalize(query_vec)[0]
    scores = matrix @ q
    k = max(1, min(int(k), scores.shape[0]))
    idx = np.argpartition(-scores, k - 1)[:k]
    idx = idx[np.argsort(-scores[idx])]
    return [(int(ids[i]), float(scores[i])) for i in idx if scores[i] >= min_score]


def vec_to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def blob_to_vec(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32, count=dim)
