"""Tools the agent may call. Owner: D.

Tools never write Cypher of their own: they call the service methods of `app/services/`.
In tests, pass fake services with the same method names.
"""

from app.services.clubs import ClubService
from app.services.gds import GdsService
from app.services.players import PlayerService
from app.services.rumours import RumourService


class ToolBox:
    """Bundles the services the agent's tools are built on."""

    def __init__(self, players: PlayerService, clubs: ClubService, gds: GdsService, rumours: RumourService):
        self.players = players
        self.clubs = clubs
        self.gds = gds
        self.rumours = rumours
