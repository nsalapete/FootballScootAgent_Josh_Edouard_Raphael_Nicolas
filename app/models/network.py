"""GDS and ML models. Owner: C."""

from typing import Literal

from pydantic import BaseModel


class ClubScore(BaseModel):
    """A club with a centrality score (PageRank by default)."""

    club_id: int
    name: str
    score: float


class Community(BaseModel):
    """A Louvain community of clubs (a transfer pipeline)."""

    community_id: int
    size: int
    clubs: list[str]


class SimilarPlayer(BaseModel):
    """A neighbour found by KNN on FastRP embeddings."""

    player_id: int
    name: str
    similarity: float


class PredictedMove(BaseModel):
    """A destination predicted by the link-prediction model. Always a hypothesis, never a fact."""

    player_id: int
    club_id: int
    club_name: str
    probability: float
    model_version: str
    nature: Literal["predicted"] = "predicted"


class GdsRefreshReport(BaseModel):
    """What `POST /api/admin/gds/refresh` recomputed."""

    projections: list[str]
    algorithms: list[str]
    duration_s: float
