"""Authenticated board recommendation route.

The strategy comes from configuration, never from the request. Ownership is
checked in the repository's board query, so a guessed board id is a 404 with
the same body as the board routes.
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.auth.dependencies import current_user
from app.auth.local import Identity
from app.boards.models import BoardNotFound
from app.recommendations.models import Mode, Recommendations
from app.recommendations.repository import RecommendationRepository
from app.recommendations.service import RecommendationService
from app.recommendations.strategy import configured
from app.search.models import SearchFilters
from app.search.repository import SearchRepository
from app.search.router import AssetCardModel, _card

router = APIRouter(prefix="/api/v1/boards", tags=["recommendations"])
User = Annotated[Identity, Depends(current_user)]


class RecommendationItemModel(AssetCardModel):
    score: float | None
    reasons: list[str]
    closest_pin_id: UUID | None


class RecommendationResponseModel(BaseModel):
    mode: Mode
    strategy: str
    fallback: Literal["browse"] | None
    profile_pin_count: int
    ignored_pin_count: int
    board_version: str
    items: list[RecommendationItemModel]


def _service(request: Request) -> RecommendationService:
    settings = request.app.state.settings
    embeddings = request.app.state.embeddings
    engine = request.app.state.db_engine
    search = SearchRepository(engine)
    return RecommendationService(
        RecommendationRepository(engine),
        lambda limit: search.browse(SearchFilters(), limit, None),
        {
            "cohesive": configured(settings.recommender_cohesive_strategy),
            "complement": configured(settings.recommender_complement_strategy),
        },
        embeddings.model_key,
        embeddings.model_version,
    )


def _response(result: Recommendations) -> RecommendationResponseModel:
    return RecommendationResponseModel(
        mode=result.mode,
        strategy=result.strategy,
        fallback=result.fallback,
        profile_pin_count=result.profile_pin_count,
        ignored_pin_count=result.ignored_pin_count,
        board_version=result.board_version,
        items=[
            RecommendationItemModel(
                **_card(item.card).model_dump(),
                score=item.score,
                reasons=list(item.reasons),
                closest_pin_id=item.closest_pin_id,
            )
            for item in result.items
        ],
    )


@router.get("/{board_id}/recommendations", response_model=RecommendationResponseModel)
def board_recommendations(
    board_id: UUID,
    request: Request,
    user: User,
    mode: Mode = Query(default="cohesive"),
    limit: int = Query(default=20, ge=1, le=50),
) -> RecommendationResponseModel:
    try:
        result = _service(request).recommend(user.id, board_id, mode, limit)
    except BoardNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return _response(result)
