import pytest
import random
from mendicot.models import Player
from mendicot.engine import MendiCotEngine

@pytest.fixture
def rng():
    return random.Random(42)

@pytest.fixture
def four_players():
    return [
        Player("P1", "TeamA", 0),
        Player("P2", "TeamB", 1),
        Player("P3", "TeamA", 2),
        Player("P4", "TeamB", 3)
    ]

@pytest.fixture
def six_players():
    return [
        Player("P1", "TeamA", 0),
        Player("P2", "TeamB", 1),
        Player("P3", "TeamA", 2),
        Player("P4", "TeamB", 3),
        Player("P5", "TeamA", 4),
        Player("P6", "TeamB", 5)
    ]

@pytest.fixture
def eight_players():
    return [
        Player("P1", "TeamA", 0),
        Player("P2", "TeamB", 1),
        Player("P3", "TeamA", 2),
        Player("P4", "TeamB", 3),
        Player("P5", "TeamA", 4),
        Player("P6", "TeamB", 5),
        Player("P7", "TeamA", 6),
        Player("P8", "TeamB", 7)
    ]

@pytest.fixture
def engine(rng):
    return MendiCotEngine(rng=rng)

@pytest.fixture
def game_state_4p(engine, four_players):
    engine.create_game("game1", four_players, hidden_trump_mode=False)
    return engine.deal_cards()

@pytest.fixture
def game_state_hidden_4p(engine, four_players):
    engine.create_game("game2", four_players, hidden_trump_mode=True)
    return engine.deal_cards()
