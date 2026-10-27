"""Text vectors for similarity search.

`tfidf` needs no API key and costs nothing. `openai` calls any OpenAI-compatible
/embeddings endpoint and gives better semantic matching.
"""
from __future__ import annotations

import logging

import httpx
import numpy as np

from app import config
from app.analysis import tfidf_matrix

log = logging.getLogger(__name__)

# Similarity band in which two chunks are "about the same thing but not copies" —
# the zone where contradictions live. Scales differ per provider.
BANDS = {
    "tfidf": {"semantic_lo": 0.30, "dup": 0.93},
    "openai": {"semantic_lo": 0.60, "dup": 0.96},
}


def provider() -> str:
    s = config.settings
    if s.embeddings_provider == "openai" and s.openai_api_key:
        return "openai"
    return "tfidf"


def embed(texts: list[str]):
    if provider() == "openai":
        try:
            return _openai_embed(texts)
        except Exception as e:  # fall back rather than failing the scan
            log.warning("embedding API failed (%s); falling back to tfidf", e)
    return tfidf_matrix(texts)


def active_band() -> dict:
    return BANDS[provider()]


def _openai_embed(texts: list[str], batch: int = 128) -> np.ndarray:
    s = config.settings
    vecs: list[list[float]] = []
    with httpx.Client(timeout=60.0) as client:
        for i in range(0, len(texts), batch):
            part = [t[:8000] for t in texts[i:i + batch]]
            r = client.post(f"{s.openai_base_url}/embeddings",
                            headers={"Authorization": f"Bearer {s.openai_api_key}"},
                            json={"model": s.embeddings_model, "input": part})
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            vecs.extend(d["embedding"] for d in data)
    m = np.asarray(vecs, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms
