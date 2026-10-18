"""Recommendation orchestration: pins -> probes -> candidates -> pool -> rank.

The repository owns ownership checks and visibility. The service never sees a
pin outside `ready_assets` in the profile, always excludes every saved pin, and
falls back to browse when the board has no usable pins.
"""

from collections.abc import Callable
from uuid import UUID

from app.recommendations.models import (
    BoardPins,
    Candidate,
    Mode,
    Recommendation,
    Recommendations,
)
from app.recommendations.repository import RecommendationRepository
from app.recommendations.retrieve import merge
from app.recommendations.strategy import Strategy
from app.search.models import AssetCard

Browse = Callable[[int], list[AssetCard]]


class RecommendationService:
    def __init__(
        self,
        repository: RecommendationRepository,
        browse: Browse,
        strategies: dict[Mode, Strategy],
        model_key: str,
        model_version: str,
    ) -> None:
        self._repository = repository
        self._browse = browse
        self._strategies = strategies
        self._model_key = model_key
        self._model_version = model_version

    def recommend(self, owner_id: UUID, board_id: UUID, mode: Mode, limit: int) -> Recommendations:
        board = self._repository.board_pins(
            owner_id, board_id, self._model_key, self._model_version
        )
        strategy = self._strategies[mode]
        if not board.profile_pins:
            return self._fallback(board, mode, strategy, limit)
        items = [
            _recommendation(scored.candidate, scored.score, board)
            for scored in self.ranked(board, strategy, limit)
        ]
        return Recommendations(
            mode=mode,
            strategy=strategy.name,
            fallback=None,
            profile_pin_count=len(board.profile_pins),
            ignored_pin_count=board.ignored_pin_count,
            board_version=board.board_version,
            items=items,
        )

    def ranked(self, board: BoardPins, strategy: Strategy, limit: int):
        """The strategy path, shared with the evaluation CLI."""
        probes = strategy.retriever.probes(board.profile_pins, strategy.pool_size(limit))
        hits = self._repository.candidates(
            probes, board.saved_ids, self._model_key, self._model_version
        )
        pool = merge(hits, probes, board.profile_pins, board.saved_ids)
        return strategy.ranker.rank(pool, limit)

    def _fallback(
        self, board: BoardPins, mode: Mode, strategy: Strategy, limit: int
    ) -> Recommendations:
        # Saved pins may still be public (an unembedded pin), so over-fetch.
        cards = [
            card for card in self._browse(limit + len(board.saved_ids))
            if card.id not in board.saved_ids
        ][:limit]
        return Recommendations(
            mode=mode,
            strategy=strategy.name,
            fallback="browse",
            profile_pin_count=0,
            ignored_pin_count=board.ignored_pin_count,
            board_version=board.board_version,
            items=[Recommendation(card, None, (), None) for card in cards],
        )


def _recommendation(candidate: Candidate, score: float, board: BoardPins) -> Recommendation:
    pins = board.profile_pins
    closest = min(
        range(len(pins)),
        key=lambda index: (-float(candidate.pin_similarity[index]), pins[index].asset_id),
    )
    reasons = ["similar_to_pin"]
    if candidate.card.creator_id is not None and any(
        pin.creator_id == candidate.card.creator_id for pin in pins
    ):
        reasons.append("same_creator")
    tags = set(candidate.style_tags)
    if tags and any(tags.intersection(pin.style_tags) for pin in pins):
        reasons.append("same_style_tag")
    return Recommendation(candidate.card, score, tuple(reasons), pins[closest].asset_id)
