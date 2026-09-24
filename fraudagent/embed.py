"""Deterministic text embeddings: signed feature hashing of word unigrams and bigrams.

Chosen so the same vectors are produced on any machine without a model
download, which keeps graph-stored embeddings reproducible. Vectors are
L2-normalised, so a dot product is the cosine similarity.
"""
from __future__ import annotations

import hashlib
import re

import numpy as np

DIM = 256
_TOKEN = re.compile(r"[a-z0-9$]+")
_STOP = {"the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "is", "was", "were", "be", "by",
         "with", "at", "this", "that", "it", "as", "from", "case", "cc"}


def _tokens(text: str) -> list[str]:
    toks = [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and not t.isdigit()]
    return toks + [a + "_" + b for a, b in zip(toks, toks[1:])]


def embed(text: str) -> list[float]:
    v = np.zeros(DIM)
    for tok in _tokens(text):
        h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:8], "little")
        v[h % DIM] += 1.0 if (h >> 63) & 1 else -1.0
    n = np.linalg.norm(v)
    return (v / n if n else v).round(5).tolist()
