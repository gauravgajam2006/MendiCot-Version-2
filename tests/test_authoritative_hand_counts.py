"""Public hand-count serialization follows the playable authoritative hand."""

import pytest
from mendicot.enums import Suit
from mendicot.models import Card


@pytest.mark.parametrize(
    ("players_fixture", "expected_initial", "expected_hidden"),
    [
        ("four_players", 12, 11),
        ("six_players", 8, 7),
        ("eight_players", 6, 5),
    ],
)
def test_hidden_trump_public_hand_counts_follow_playable_hands(
    request, engine, players_fixture, expected_initial, expected_hidden
):
    players = request.getfixturevalue(players_fixture)
    hider_id = players[0].player_id
    observer_id = players[1].player_id

    engine.create_game("hand-counts", players, hider_id, hidden_trump_mode=True)
    engine.select_trump_hider(hider_id, hider_id)
    engine.deal_cards()

    initial_view = engine.get_player_view(observer_id)
    assert initial_view["hand_counts"] == {
        player.player_id: expected_initial for player in players
    }

    engine.select_hidden_card(hider_id, 0)
    hidden_view = engine.get_player_view(observer_id)
    assert hidden_view["hand_counts"][hider_id] == expected_hidden
    assert all(
        hidden_view["hand_counts"][player.player_id] == expected_initial
        for player in players[1:]
    )
    # The numeric count does not introduce any hidden-card identity leakage.
    assert list(hidden_view["hands"]) == [observer_id]
    assert hidden_view["trump_state"]["hidden_rank"] is None
    assert hidden_view["trump_state"]["hidden_card_index"] is None
    assert hidden_view["trump_state"]["suit"] is None

    engine.complete_hidden_trump_setup()
    lead_card = engine.state.hands[observer_id][0]
    engine.play_card(observer_id, lead_card)
    revealer_id = players[2].player_id
    non_lead_suit = next(suit for suit in Suit if suit != lead_card.suit)
    engine.state.hands[revealer_id] = [Card(non_lead_suit, 3)]
    engine.reveal_trump(revealer_id)
    engine.complete_trump_reveal_display()
    engine.complete_hidden_card_return()
    returned_view = engine.get_player_view(observer_id)
    assert returned_view["hand_counts"][hider_id] == expected_initial


@pytest.mark.parametrize(
    ("players_fixture", "expected_count"),
    [
        ("four_players", 12),
        ("six_players", 8),
        ("eight_players", 6),
    ],
)
def test_normal_trump_public_hand_counts_are_unchanged(
    request, engine, players_fixture, expected_count
):
    players = request.getfixturevalue(players_fixture)
    engine.create_game("normal-hand-counts", players, players[0].player_id)
    engine.deal_cards()

    view = engine.get_player_view(players[1].player_id)
    assert view["hand_counts"] == {
        player.player_id: expected_count for player in players
    }

    player_id = players[0].player_id
    engine.play_card(player_id, engine.state.hands[player_id][0])
    updated_view = engine.get_player_view(players[1].player_id)
