"""Club-name rules shared by the agent and the rumour import. Owner: D.

Both were found on the real graph, where the mini graph had no such case: the full-text index finds nothing for
"PSG", and "Benfica" or "Real Madrid" also match their youth and reserve teams, so a name that should resolve to one
club looked ambiguous.
"""

import re

# Youth, reserve and B teams in Transfermarkt club names ("Ajax U19", "RM Castilla", "Barcelona B", "Jong PSV").
# Not "II": Willem II is a first team.
SECOND_TEAM = (
    r"(\bU-?\d{2}\b|\bSub-?\d{2}\b|\s[BC]$|\bCastilla\b|\bYouth\b|\bReserves?\b|\bPrimavera\b|\bJuvenil|"
    + r"\bAcademy\b|^Jong\s|\bPromesas\b|\bAmateure\b)"
)

# Generic words of club names, ignored when matching a name from an article ("Liverpool Football Club",
# "Juventus F.C.").
CLUB_FILLER = frozenset({"football", "club", "fc", "cf", "afc", "f", "c"})

# Nicknames the full-text index cannot find, mapped to the Transfermarkt club name. Only unambiguous ones.
CLUB_NICKNAMES = {
    "psg": "Paris Saint-Germain",
    "paris sg": "Paris Saint-Germain",
    "man utd": "Manchester United",
    "man united": "Manchester United",
    "man city": "Manchester City",
    "spurs": "Tottenham Hotspur",
    "barça": "FC Barcelona",
    "barca": "FC Barcelona",
    "juve": "Juventus FC",
    "bvb": "Borussia Dortmund",
    "gladbach": "Borussia Mönchengladbach",
    "bayern münchen": "Bayern Munich",
    "fc bayern münchen": "Bayern Munich",
    "fc bayern munich": "Bayern Munich",
    "atleti": "Atlético de Madrid",
    "om": "Olympique Marseille",
    "ol": "Olympique Lyon",
}


def is_second_team(name: str) -> bool:
    """Youth, reserve and B teams ("Benfica U19", "Real Madrid Castilla", "Barcelona B")."""
    return re.search(SECOND_TEAM, name.strip(), re.IGNORECASE) is not None


def expand_nickname(name: str) -> str:
    """The Transfermarkt name of a known club nickname, or `name` unchanged."""
    return CLUB_NICKNAMES.get(name.strip().casefold(), name)


def prefer_first_teams(name: str, names: list[str]) -> list[int]:
    """Indexes of `names` to keep: first teams only, unless `name` itself names a youth or reserve team (or no first
    team is left, in which case nothing is dropped)."""
    keep = list(range(len(names)))
    if is_second_team(name):
        return keep
    first = [index for index in keep if not is_second_team(names[index])]
    return first or keep
