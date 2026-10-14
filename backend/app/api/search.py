"""Public catalog routes. Nothing here reads the negative cache.

Sync handlers run in FastAPI's threadpool, so a text embedding does not block
the event loop. Query vectors are computed for the request and not stored.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.search.cursor import InvalidCursor
from app.search.service import (
    AssetCard,
    AssetDetail,
    CreatorPage,
    RelatedHit,
    SearchPage,
    SearchQueryError,
    SearchService,
)

router = APIRouter(prefix="/api/v1")

_LIMIT = Query(default=20, ge=1, le=50)
_RELATED_LIMIT = Query(default=12, ge=1, le=50)


class AssetCardModel(BaseModel):
    id: UUID
    title: str | None
    creator_id: UUID | None
    creator_name: str | None
    location_label: str | None
    created_year_start: int | None
    created_year_end: int | None
    thumbnail_key: str
    ingested_at: str


class SearchResponseModel(BaseModel):
    applied_query: str
    suggestion: str | None
    items: list[AssetCardModel]
    next_cursor: str | None


class AssetDetailModel(AssetCardModel):
    description: str | None
    original_key: str
    width: int
    height: int
    landing_url: str | None
    rights_type: str
    evidence_url: str | None
    verified_at: str


class RelatedItemModel(AssetCardModel):
    reasons: list[str]


class RelatedResponseModel(BaseModel):
    items: list[RelatedItemModel]


class CreatorModel(BaseModel):
    id: UUID
    name: str


class CreatorResponseModel(BaseModel):
    creators: list[CreatorModel]
    did_you_mean: str | None


def _service(request: Request) -> SearchService:
    return SearchService(request.app.state.db_engine, request.app.state.embeddings)


def _card(card: AssetCard) -> AssetCardModel:
    return AssetCardModel(
        id=card.id,
        title=card.title,
        creator_id=card.creator_id,
        creator_name=card.creator_name,
        location_label=card.location_label,
        created_year_start=card.created_year_start,
        created_year_end=card.created_year_end,
        thumbnail_key=card.thumbnail_key,
        ingested_at=card.ingested_at.isoformat(),
    )


def _page(page: SearchPage) -> SearchResponseModel:
    return SearchResponseModel(
        applied_query=page.applied_query,
        suggestion=page.suggestion,
        items=[_card(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/assets", response_model=SearchResponseModel)
def search_assets(
    request: Request,
    q: str | None = None,
    creator_id: UUID | None = None,
    location: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int = _LIMIT,
    cursor: str | None = None,
) -> SearchResponseModel:
    try:
        page = _service(request).search(
            q=q,
            creator_id=creator_id,
            location=location,
            year_from=year_from,
            year_to=year_to,
            limit=limit,
            cursor=cursor,
        )
    except SearchQueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except InvalidCursor as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc
    return _page(page)


@router.get("/assets/{asset_id}", response_model=AssetDetailModel)
def asset_detail(asset_id: UUID, request: Request) -> AssetDetailModel:
    detail: AssetDetail | None = _service(request).asset(asset_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="asset not found")
    card = _card(detail.card)
    return AssetDetailModel(
        **card.model_dump(),
        description=detail.description,
        original_key=detail.original_key,
        width=detail.width,
        height=detail.height,
        landing_url=detail.landing_url,
        rights_type=detail.rights_type,
        evidence_url=detail.evidence_url,
        verified_at=detail.verified_at.isoformat(),
    )


@router.get("/assets/{asset_id}/related", response_model=RelatedResponseModel)
def related_assets(
    asset_id: UUID,
    request: Request,
    limit: int = _RELATED_LIMIT,
) -> RelatedResponseModel:
    hits: list[RelatedHit] | None = _service(request).related(asset_id, limit)
    if hits is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return RelatedResponseModel(
        items=[
            RelatedItemModel(**_card(hit.card).model_dump(), reasons=list(hit.reasons))
            for hit in hits
        ]
    )


@router.get("/creators", response_model=CreatorResponseModel)
def creators(
    request: Request,
    query: str | None = None,
    limit: int = _LIMIT,
) -> CreatorResponseModel:
    page: CreatorPage = _service(request).creators(query, limit)
    return CreatorResponseModel(
        creators=[CreatorModel(id=creator.id, name=creator.name) for creator in page.creators],
        did_you_mean=page.did_you_mean,
    )
