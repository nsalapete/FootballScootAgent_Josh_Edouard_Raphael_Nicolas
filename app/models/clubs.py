"""Club models. Owner: B."""

from pydantic import BaseModel


class PipelineRow(BaseModel):
    """Outgoing transfers of a club, aggregated by destination competition."""

    competition_id: str
    competition_name: str
    transfers: int
    total_fee_eur: int
