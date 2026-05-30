# esd/sofascore/endpoints.py

"""
This module contains the endpoints of the SofaScore API.
"""

class SofascoreEndpoints:
    """
    A class to represent the endpoints of the SofaScore API.
    """

    def __init__(self, base_url: str = "https://api.sofascore.com/api/v1") -> None:
        self.base_url = base_url

    @property
    def events_endpoint(self) -> str:
        return self.base_url + "/sport/football/scheduled-events/{date}"

    @property
    def live_events_endpoint(self) -> str:
        return self.base_url + "/sport/football/events/live"

    def event_endpoint(self, event_id: int) -> str:
        return f"{self.base_url}/event/{event_id}"

    def search_endpoint(self, query: str, entity_type: str) -> str:
        return f"{self.base_url}/search/{entity_type}?q={query}&page=0"

    def player_endpoint(self, player_id: int) -> str:
        return f"{self.base_url}/player/{player_id}"

    def player_transfer_history_endpoint(self, player_id: int) -> str:
        return f"{self.base_url}/player/{player_id}/transfer-history"

    def player_charac_endpoint(self, player_id: int) -> str:
        return f"{self.base_url}/player/{player_id}/characteristics"

    def player_attributes_endpoint(self, player_id: int) -> str:
        return f"{self.base_url}/player/{player_id}/attribute-overviews"

    def player_stats_endpoint(self, player_id: int) -> str:
        return f"{self.base_url}/player/{player_id}/statistics"

    def team_endpoint(self, team_id: int) -> str:
        return f"{self.base_url}/team/{team_id}"

    def team_players_endpoint(self, team_id: int) -> str:
        return self.team_endpoint(team_id) + "/players"

    def team_events_endpoint(self, team_id: int, upcoming: bool, page: int) -> str:
        _from = "last" if not upcoming else "next"
        return f"{self.base_url}/team/{team_id}/events/{_from}/{page}"

    # --- Match Stats Target Endpoints Verified Method Inputs ---
    def match_stats_endpoint(self, event_id: int) -> str:
        return f"{self.base_url}/event/{event_id}/statistics"

    def match_events_endpoint(self, event_id: int) -> str:
        return f"{self.base_url}/event/{event_id}/incidents"

    def match_top_players_endpoint(self, event_id: int) -> str:
        return f"{self.base_url}/event/{event_id}/best-players/summary"

    def match_comments_endpoint(self, event_id: int) -> str:
        return f"{self.base_url}/event/{event_id}/comments"

    def match_shots_endpoint(self, event_id: int) -> str:
        return f"{self.base_url}/event/{event_id}/shotmap"

    def match_probabilities_endpoint(self, event_id: int) -> str:
        return f"{self.base_url}/event/{event_id}/win-probability"

    def match_lineups_endpoint(self, event_id: int) -> str:
        return f"{self.base_url}/event/{event_id}/lineups"

    def tournaments_endpoint(self, category_id: int) -> str:
        return f"{self.base_url}/category/{category_id}/unique-tournaments"

    def tournament_seasons_endpoint(self, tournament_id: int) -> str:
        return f"{self.base_url}/unique-tournament/{tournament_id}/seasons"

    def tournament_bracket_endpoint(self, tournament_id: int, season_id: int) -> str:
        return f"{self.base_url}/unique-tournament/{tournament_id}/season/{season_id}/cuptrees"

    def tournament_standings_endpoint(self, tournament_id: int, season_id: int) -> str:
        base = self.base_url + "/unique-tournament"
        return f"{base}/{tournament_id}/season/{season_id}/standings/total"

    def tournament_topteams_endpoint(self, tournament_id: int, season_id: int) -> str:
        base = self.base_url + "/unique-tournament"
        return f"{base}/{tournament_id}/season/{season_id}/top-teams/overall"

    def tournament_topplayers_endpoint(self, tournament_id: int, season_id: int) -> str:
        base = self.base_url + "/unique-tournament"
        return f"{base}/{tournament_id}/season/{season_id}/top-players/overall"

    def tournament_events_endpoint(self, tournament_id: int, season_id: int, upcoming: bool, page: int) -> str:
        _from = "last" if not upcoming else "next"
        return f"{self.base_url}/unique-tournament/{tournament_id}/season/{season_id}/events/{_from}/{page}"
