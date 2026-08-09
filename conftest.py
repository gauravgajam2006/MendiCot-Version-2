import inspect

import httpx
import pytest
from mendicot.secure_shuffle import DeterministicEntropySource
from mendicot.models import Player
from mendicot.engine import MendiCotEngine


# Starlette 0.27 passes the ASGI app to HTTPX's former convenience argument.
# Firebase Admin 7.5 requires HTTPX 0.28, which removed only that argument;
# Starlette still supplies its own transport, so ignoring ``app`` restores the
# original test-only behavior without changing application runtime code.
if "app" not in inspect.signature(httpx.Client.__init__).parameters:
    _httpx_client_init = httpx.Client.__init__

    def _compatible_httpx_client_init(self, *args, app=None, **kwargs):
        _httpx_client_init(self, *args, **kwargs)

    httpx.Client.__init__ = _compatible_httpx_client_init

@pytest.fixture
def entropy_source():
    return DeterministicEntropySource(b"mendicot-test-suite-seed")

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
def engine(entropy_source):
    return MendiCotEngine(entropy_source=entropy_source)

@pytest.fixture
def game_state_4p(engine, four_players):
    engine.create_game(
    "game1",
    four_players,
    host_id=four_players[0].player_id,
    hidden_trump_mode=False
)
    return engine.deal_cards()

@pytest.fixture
def game_state_hidden_4p(engine, four_players):
    engine.create_game(
        "game2",
        four_players,
        host_id=four_players[0].player_id,
        hidden_trump_mode=True
    )

    engine.select_trump_hider("P1", "P1")

    return engine.deal_cards()
