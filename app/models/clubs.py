"""Club models. Owner: B."""

from pydantic import BaseModel, ConfigDict


class PipelineRow(BaseModel):
    """Outgoing transfers of a club, aggregated by destination competition."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "competition_id": "GB1",
                    "competition_name": "Premier League",
                    "transfers": 2,
                    "total_fee_eur": 50000000,
                }
            ]
        }
    )

    competition_id: str
    competition_name: str
    transfers: int
    total_fee_eur: int
