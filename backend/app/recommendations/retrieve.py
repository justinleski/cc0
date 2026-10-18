"""Retrievers turn profile pins into probes; merge turns hits into a pool.

Pure functions with no randomness. Pin order never decides which pins become
probes: per-pin uses every pin with an equal budget, and clustering orders pins
by asset id before it starts. Retrieval is evaluated separately from ranking,
so a retriever must not reorder or score candidates.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

import numpy as np

from app.recommendations.models import (
    Candidate,
    CandidateHit,
    CandidatePool,
    Probe,
    ProfilePin,
)


class Retriever(Protocol):
    name: str

    def probes(self, pins: Sequence[ProfilePin], pool: int) -> list[Probe]: ...


def normalize(vector: np.ndarray) -> np.ndarray | None:
    norm = float(np.linalg.norm(vector))
    return None if norm < 1e-12 else vector / norm


def centroid_vector(pins: Sequence[ProfilePin]) -> np.ndarray:
    """The L2-normalized mean. A single pin is its own vector."""
    if len(pins) == 1:
        return pins[0].vector
    mean = normalize(np.mean([pin.vector for pin in pins], axis=0))
    # Opposing vectors can cancel. Fall back to the first pin by asset id
    # rather than probing with a meaningless direction.
    if mean is None:
        return min(pins, key=lambda pin: pin.asset_id).vector
    return mean


@dataclass(frozen=True)
class CentroidRetriever:
    name: str = "centroid"

    def probes(self, pins: Sequence[ProfilePin], pool: int) -> list[Probe]:
        if not pins:
            return []
        ids = tuple(sorted(pin.asset_id for pin in pins))
        return [Probe(centroid_vector(pins), ids, pool)]


@dataclass(frozen=True)
class PerPinRetriever:
    """Every pin is a probe with the same budget. No cap, so no pin is favored."""

    floor: int = 5
    name: str = "per-pin"

    def probes(self, pins: Sequence[ProfilePin], pool: int) -> list[Probe]:
        if not pins:
            return []
        budget = max(self.floor, math.ceil(pool / len(pins)))
        ordered = sorted(pins, key=lambda pin: pin.asset_id)
        return [Probe(pin.vector, (pin.asset_id,), budget) for pin in ordered]


@dataclass(frozen=True)
class ClusterRetriever:
    """One probe per cluster when the pins separate; otherwise the centroid."""

    max_k: int = 4
    min_cluster_size: int = 2
    silhouette_threshold: float = 0.15
    min_separation: float = 0.15
    budget: Literal["equal", "proportional"] = "equal"
    floor: int = 5
    name: str = "cluster"

    def probes(self, pins: Sequence[ProfilePin], pool: int) -> list[Probe]:
        if not pins:
            return []
        ordered = sorted(pins, key=lambda pin: pin.asset_id)
        vectors = np.stack([pin.vector for pin in ordered])
        labels = choose_clusters(
            vectors,
            self.max_k,
            self.min_cluster_size,
            self.silhouette_threshold,
            self.min_separation,
        )
        groups: dict[int, list[ProfilePin]] = {}
        for label, pin in zip(labels, ordered, strict=True):
            groups.setdefault(int(label), []).append(pin)
        # Order clusters by their smallest asset id so probe indexes are stable.
        clusters = sorted(groups.values(), key=lambda group: group[0].asset_id)
        probes = []
        for group in clusters:
            share = 1 / len(clusters)
            if self.budget == "proportional":
                share = len(group) / len(ordered)
            probes.append(
                Probe(
                    centroid_vector(group),
                    tuple(pin.asset_id for pin in group),
                    max(self.floor, math.ceil(pool * share)),
                )
            )
        return probes


def spherical_kmeans(vectors: np.ndarray, k: int, iterations: int = 25) -> np.ndarray:
    """Deterministic k-means on unit vectors with farthest-first initialization.

    The first center is the row nearest the centroid; each next center is the
    row least similar to every chosen center. `argmax`/`argmin` take the lowest
    index on ties, and callers order rows by asset id.
    """
    mean = normalize(vectors.mean(axis=0))
    first = 0 if mean is None else int(np.argmax(vectors @ mean))
    centers = [vectors[first]]
    while len(centers) < k:
        nearest = np.max(vectors @ np.stack(centers).T, axis=1)
        centers.append(vectors[int(np.argmin(nearest))])
    center_matrix = np.stack(centers)
    labels = np.zeros(len(vectors), dtype=int)
    for _ in range(iterations):
        updated = np.argmax(vectors @ center_matrix.T, axis=1)
        for index in range(k):
            members = vectors[updated == index]
            if len(members):
                moved = normalize(members.mean(axis=0))
                if moved is not None:
                    center_matrix[index] = moved
        if np.array_equal(updated, labels):
            break
        labels = updated
    return labels


def silhouette(vectors: np.ndarray, labels: np.ndarray) -> float:
    """Mean silhouette with cosine distance. Singletons score 0, as usual."""
    distance = 1.0 - vectors @ vectors.T
    scores = []
    for index, label in enumerate(labels):
        same = labels == label
        same[index] = False
        if not same.any():
            scores.append(0.0)
            continue
        inner = distance[index, same].mean()
        outer = min(
            distance[index, labels == other].mean()
            for other in set(labels.tolist())
            if other != label
        )
        scores.append((outer - inner) / max(inner, outer, 1e-12))
    return float(np.mean(scores))


def separation(vectors: np.ndarray, labels: np.ndarray) -> float:
    """The smallest cosine distance between any two cluster centroids."""
    centers = []
    for label in sorted(set(labels.tolist())):
        center = normalize(vectors[labels == label].mean(axis=0))
        if center is None:
            return 0.0
        centers.append(center)
    matrix = np.stack(centers)
    similarity = matrix @ matrix.T
    upper = similarity[np.triu_indices(len(centers), k=1)]
    return float(1.0 - upper.max())


def choose_clusters(
    vectors: np.ndarray,
    max_k: int,
    min_cluster_size: int,
    threshold: float,
    min_separation: float,
) -> np.ndarray:
    """k=1 unless some k separates the pins clearly. Ties prefer the smaller k.

    Silhouette is scale-free: one tight style split in half can still score
    well. `min_separation` is an absolute floor on centroid distance, so noise
    inside one style is not mistaken for two interests.
    """
    best = np.zeros(len(vectors), dtype=int)
    best_score = threshold
    for k in range(2, min(max_k, len(vectors) // min_cluster_size) + 1):
        labels = spherical_kmeans(vectors, k)
        if np.bincount(labels, minlength=k).min() < min_cluster_size:
            continue
        if separation(vectors, labels) < min_separation:
            continue
        score = silhouette(vectors, labels)
        if score >= best_score and (best.max() == 0 or score > best_score):
            best, best_score = labels, score
    return best


def merge(
    hits: Sequence[CandidateHit],
    probes: Sequence[Probe],
    pins: Sequence[ProfilePin],
    saved_ids: frozenset[UUID],
) -> CandidatePool:
    """Deduplicate hits, drop saved pins again, and record every similarity."""
    by_id: dict[UUID, CandidateHit] = {}
    sources: dict[UUID, set[int]] = {}
    for hit in hits:
        if hit.card.id in saved_ids:
            continue
        by_id.setdefault(hit.card.id, hit)
        sources.setdefault(hit.card.id, set()).add(hit.probe_index)
    probe_matrix = np.stack([probe.vector for probe in probes]) if probes else np.zeros((0, 0))
    pin_matrix = np.stack([pin.vector for pin in pins]) if pins else np.zeros((0, 0))
    candidates = []
    for asset_id in sorted(by_id):
        hit = by_id[asset_id]
        candidates.append(
            Candidate(
                card=hit.card,
                style_tags=hit.style_tags,
                vector=hit.vector,
                sources=tuple(sorted(sources[asset_id])),
                probe_similarity=probe_matrix @ hit.vector if probes else np.zeros(0),
                pin_similarity=pin_matrix @ hit.vector if pins else np.zeros(0),
            )
        )
    return CandidatePool(tuple(probes), tuple(pins), tuple(candidates))
