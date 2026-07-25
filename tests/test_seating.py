import pytest
from mendicot.models import Player
from mendicot.validators import validate_seating, validate_team_configuration
from mendicot.exceptions import InvalidSeatArrangement, InvalidTeamConfiguration

def test_alternating_4_players_accepted(four_players):
    validate_seating(four_players)

def test_alternating_6_players_accepted(six_players):
    validate_seating(six_players)

def test_alternating_8_players_accepted(eight_players):
    validate_seating(eight_players)

def test_same_team_adjacent_rejected():
    players = [
        Player("P1", "TeamA", 0),
        Player("P2", "TeamA", 1),
        Player("P3", "TeamB", 2),
        Player("P4", "TeamB", 3)
    ]
    with pytest.raises(InvalidSeatArrangement):
        validate_seating(players)

def test_wrap_around_adjacency_rejected():
    # Last B wraps to first B or A to A
    players = [
        Player("P1", "TeamA", 0),
        Player("P2", "TeamB", 1),
        Player("P3", "TeamB", 2),
        Player("P4", "TeamA", 3)
    ]
    with pytest.raises(InvalidSeatArrangement):
        validate_seating(players)

def test_unequal_teams_rejected():
    players = [
        Player("P1", "TeamA", 0),
        Player("P2", "TeamA", 1),
        Player("P3", "TeamA", 2),
        Player("P4", "TeamB", 3)
    ]
    with pytest.raises(InvalidTeamConfiguration):
        validate_team_configuration(players)
