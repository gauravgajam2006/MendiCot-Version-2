from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from mendicot.api import routes
from mendicot.engine import MendiCotEngine
from mendicot.enums import GamePhase, Rank, Suit
from mendicot.models import Card, PlayedCard, Player, Trick


client = TestClient(routes.app)
EMPTY_MENDIS = {"TeamA": [], "TeamB": []}
CANONICAL_SUITS = ["SPADES", "HEARTS", "DIAMONDS", "CLUBS"]


def players_for(count):
    return [
        Player(
            player_id=f"P{index + 1}",
            team_id="TeamA" if index % 2 == 0 else "TeamB",
            seat_index=index,
        )
        for index in range(count)
    ]


def create_engine(player_count=4, hidden_trump_mode=False):
    engine = MendiCotEngine()
    players = players_for(player_count)
    engine.create_game(
        "captured-mendis",
        players,
        host_id="P1",
        hidden_trump_mode=hidden_trump_mode,
    )
    return engine


def resolving_trick(cards):
    return Trick(
        lead_player_id=cards[0][0],
        lead_suit=cards[0][1].suit,
        played_cards=[
            PlayedCard(player_id, card) for player_id, card in cards
        ],
        completed=True,
    )


def resolve_cards(engine, cards):
    engine.state.phase = GamePhase.TRICK_RESOLUTION
    engine.state.current_trick = resolving_trick(cards)
    return engine.resolve_trick()


def mendi_count_invariant(state):
    return {
        team_id: team.tens_captured
        == len(state.captured_mendis[team_id])
        for team_id, team in state.teams.items()
    }


def _clear_api_state():
    for task in list(routes._disconnect_cleanup_tasks.values()):
        task.cancel()
    routes._disconnect_cleanup_tasks.clear()
    for task in list(routes._trick_resolution_tasks.values()):
        task.cancel()
    routes._trick_resolution_tasks.clear()
    routes.room_manager._rooms.clear()
    routes.connection_manager.active_connections.clear()
    routes.session_tokens.clear()
    routes.invalidated_session_tokens.clear()


@pytest.fixture(autouse=True)
def reset_api_state():
    _clear_api_state()
    yield
    _clear_api_state()


def receive_type(websocket, message_type, predicate=None, max_messages=100):
    for _ in range(max_messages):
        message = websocket.receive_json()
        if message["type"] == message_type and (
            predicate is None or predicate(message)
        ):
            return message
    raise AssertionError(f"Did not receive {message_type}")


def test_initial_player_view_has_explicit_empty_team_entries():
    engine = create_engine()

    assert engine.state.captured_mendis == EMPTY_MENDIS
    assert engine.get_player_view("P1")["captured_mendis"] == EMPTY_MENDIS


def test_single_and_multiple_mendis_are_awarded_to_winning_teams():
    engine = create_engine()
    resolve_cards(
        engine,
        [
            ("P1", Card(Suit.SPADES, Rank.TEN)),
            ("P2", Card(Suit.SPADES, Rank.ACE)),
            ("P3", Card(Suit.HEARTS, Rank.THREE)),
            ("P4", Card(Suit.CLUBS, Rank.THREE)),
        ],
    )
    assert engine.get_player_view("P1")["captured_mendis"] == {
        "TeamA": [],
        "TeamB": ["SPADES"],
    }

    resolve_cards(
        engine,
        [
            ("P1", Card(Suit.HEARTS, Rank.ACE)),
            ("P2", Card(Suit.SPADES, Rank.THREE)),
            ("P3", Card(Suit.DIAMONDS, Rank.TEN)),
            ("P4", Card(Suit.CLUBS, Rank.TEN)),
        ],
    )
    assert engine.get_player_view("P1")["captured_mendis"] == {
        "TeamA": ["DIAMONDS", "CLUBS"],
        "TeamB": ["SPADES"],
    }
    assert all(mendi_count_invariant(engine.state).values())


def test_ordinary_trick_does_not_change_captured_mendis():
    engine = create_engine()
    resolve_cards(
        engine,
        [
            ("P1", Card(Suit.HEARTS, Rank.ACE)),
            ("P2", Card(Suit.HEARTS, Rank.KING)),
            ("P3", Card(Suit.HEARTS, Rank.NINE)),
            ("P4", Card(Suit.HEARTS, Rank.THREE)),
        ],
    )

    assert engine.get_player_view("P1")["captured_mendis"] == EMPTY_MENDIS
    assert all(mendi_count_invariant(engine.state).values())


def test_mendi_suit_is_globally_unique_and_cannot_move_between_teams():
    engine = create_engine()
    resolve_cards(
        engine,
        [
            ("P1", Card(Suit.SPADES, Rank.ACE)),
            ("P2", Card(Suit.HEARTS, Rank.TEN)),
            ("P3", Card(Suit.DIAMONDS, Rank.THREE)),
            ("P4", Card(Suit.CLUBS, Rank.THREE)),
        ],
    )
    resolve_cards(
        engine,
        [
            ("P1", Card(Suit.CLUBS, Rank.THREE)),
            ("P2", Card(Suit.CLUBS, Rank.ACE)),
            ("P3", Card(Suit.HEARTS, Rank.TEN)),
            ("P4", Card(Suit.DIAMONDS, Rank.THREE)),
        ],
    )

    view = engine.get_player_view("P1")
    assert view["captured_mendis"] == {
        "TeamA": ["HEARTS"],
        "TeamB": [],
    }
    all_suits = [
        suit
        for suits in view["captured_mendis"].values()
        for suit in suits
    ]
    assert len(all_suits) == len(set(all_suits))
    assert all(mendi_count_invariant(engine.state).values())


def test_serialization_uses_canonical_suit_order():
    engine = create_engine()
    engine.state.captured_mendis["TeamA"] = [
        Suit.CLUBS,
        Suit.DIAMONDS,
        Suit.HEARTS,
        Suit.SPADES,
    ]
    engine.state.teams["TeamA"].tens_captured = 4

    assert (
        engine.get_player_view("P1")["captured_mendis"]["TeamA"]
        == CANONICAL_SUITS
    )


@pytest.mark.parametrize("player_count", [4, 6, 8])
@pytest.mark.parametrize("hidden_trump_mode", [False, True])
def test_resolution_timing_modes_and_player_counts(
    player_count, hidden_trump_mode
):
    engine = create_engine(player_count, hidden_trump_mode)
    state = engine.state
    state.phase = GamePhase.PLAYING
    state.current_turn = "P1"
    state.hands = {
        player.player_id: [
            Card(
                Suit.SPADES,
                Rank.TEN
                if player.player_id == "P1"
                else Rank.ACE
                if player.player_id == "P2"
                else Rank.THREE,
            )
        ]
        for player in state.players
    }

    for _ in range(player_count):
        player_id = state.current_turn
        engine.play_card(player_id, state.hands[player_id][0])

    assert state.phase == GamePhase.TRICK_RESOLUTION
    assert engine.get_player_view("P1")["captured_mendis"] == EMPTY_MENDIS
    assert sum(team.tens_captured for team in state.teams.values()) == 0

    engine.resolve_trick()

    assert state.phase == GamePhase.FINAL_SCORE_DISPLAY
    assert state.final_result == "TeamB"
    assert engine.get_player_view("P1")["captured_mendis"] == {
        "TeamA": [],
        "TeamB": ["SPADES"],
    }
    assert all(mendi_count_invariant(state).values())
    engine.finalize_game()
    assert state.phase == GamePhase.GAME_OVER


def test_game_over_and_draw_views_preserve_captured_mendis():
    engine = create_engine()
    engine.state.captured_mendis = {
        "TeamA": [Suit.SPADES],
        "TeamB": [Suit.HEARTS],
    }
    engine.state.teams["TeamA"].tens_captured = 1
    engine.state.teams["TeamB"].tens_captured = 1

    for phase in (GamePhase.GAME_OVER, GamePhase.DRAW):
        engine.state.phase = phase
        assert engine.get_player_view("P1")["captured_mendis"] == {
            "TeamA": ["SPADES"],
            "TeamB": ["HEARTS"],
        }


def test_reconnecting_player_immediately_receives_captured_mendis():
    response = client.post(
        "/api/rooms",
        json={"player_count": 4, "trump_mode": "normal"},
    )
    room_id = response.json()["room_id"]
    joined = []
    for index in range(4):
        response = client.post(
            f"/api/rooms/{room_id}/join",
            json={
                "player_id": f"P{index + 1}",
                "display_name": f"Player {index + 1}",
            },
        )
        joined.append(response.json())

    with ExitStack() as stack:
        sockets = []
        for player in joined:
            websocket = stack.enter_context(
                client.websocket_connect(
                    f"/ws/rooms/{room_id}?token={player['session_token']}"
                )
            )
            receive_type(websocket, "ROOM_STATE_UPDATE")
            sockets.append(websocket)

        host = sockets[0]
        host.send_json({"action": "START_GAME"})
        receive_type(host, "ACTION_SUCCESS")
        host.send_json(
            {
                "action": "SELECT_FIRST_PLAYER",
                "payload": {"player_id": "P1"},
            }
        )
        receive_type(host, "ACTION_SUCCESS")

        room = routes.room_manager.get_room(room_id)
        resolve_cards(
            room.engine,
            [
                ("P1", Card(Suit.SPADES, Rank.ACE)),
                ("P2", Card(Suit.HEARTS, Rank.TEN)),
                ("P3", Card(Suit.DIAMONDS, Rank.THREE)),
                ("P4", Card(Suit.CLUBS, Rank.THREE)),
            ],
        )

        with client.websocket_connect(
            f"/ws/rooms/{room_id}?token={joined[0]['session_token']}"
        ) as replacement:
            snapshot = receive_type(
                replacement, "GAME_STATE_UPDATE"
            )["payload"]

        assert snapshot["captured_mendis"] == {
            "TeamA": ["HEARTS"],
            "TeamB": [],
        }
        assert snapshot["teams"]["TeamA"]["tens_captured"] == 1