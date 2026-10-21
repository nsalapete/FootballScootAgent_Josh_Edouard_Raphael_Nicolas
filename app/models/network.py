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
    # The probability is the one of the pair (current club, destination): same answer for every player of a club.
    basis: Literal["club_pair"] = "club_pair"
    horizon_seasons: int | None = None  # the pair trades within this many seasons (None for older predictions)
    trained_until: int | None = None  # last season whose outcome the model learnt from


class GdsRefreshReport(BaseModel):
    """What `POST /api/admin/gds/refresh` recomputed."""

    projections: list[str]
    algorithms: list[str]
    duration_s: float
