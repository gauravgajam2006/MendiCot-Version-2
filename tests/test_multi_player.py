import pytest
from mendicot.enums import GamePhase
from mendicot.models import Player
from mendicot.validators import validate_seating
from mendicot.exceptions import InvalidSeatArrangement


@pytest.mark.parametrize("player_count,expected_team_size", [(6, 3), (8, 4)])
def test_create_valid_multiplayer_game(engine, six_players, eight_players, player_count, expected_team_size):
    players = six_players if player_count == 6 else eight_players
    state = engine.create_game(
        f"game_{player_count}p",
        players,
        host_id=players[0].player_id
    )

    assert state.player_count == player_count
    assert len(state.players) == player_count
    assert len(state.teams) == 2
    assert len(state.teams["TeamA"].player_ids) == expected_team_size
    assert len(state.teams["TeamB"].player_ids) == expected_team_size
    assert state.seat_order == [p.player_id for p in players]


@pytest.mark.parametrize("player_count", [6, 8])
def test_seating_validation_multiplayer(six_players, eight_players, player_count):
    players = six_players if player_count == 6 else eight_players
    # Valid alternating seating should pass
    validate_seating(players)

    # Invalid seating (two adjacent players on same team)
    invalid_players = list(players)
    invalid_players[1] = Player(invalid_players[1].player_id, "TeamA", invalid_players[1].seat_index)
    with pytest.raises(InvalidSeatArrangement):
        validate_seating(invalid_players)


@pytest.mark.parametrize("player_count,expected_cards_per_player", [(6, 8), (8, 6)])
def test_card_dealing_distribution_and_integrity(engine, six_players, eight_players, player_count, expected_cards_per_player):
    players = six_players if player_count == 6 else eight_players
    engine.create_game(
        f"game_deal_{player_count}p",
        players,
        host_id=players[0].player_id
    )
    state = engine.deal_cards()

    assert len(state.hands) == player_count

    all_dealt_cards = []
    for player_id, hand in state.hands.items():
        assert len(hand) == expected_cards_per_player
        all_dealt_cards.extend(hand)

    # Verify 6p (6x8=48) and 8p (8x6=48) have no duplicate/lost cards, 48 unique cards total
    assert len(all_dealt_cards) == 48
    assert len(set(all_dealt_cards)) == 48


@pytest.mark.parametrize("player_count", [6, 8])
def test_host_can_select_trump_hider_and_first_player(engine, six_players, eight_players, player_count):
    players = six_players if player_count == 6 else eight_players
    engine.create_game(
        f"game_host_select_{player_count}p",
        players,
        host_id=players[0].player_id,
        hidden_trump_mode=True,
    )

    hider_id = players[2].player_id
    first_player_id = players[4].player_id

    engine.select_trump_hider(players[0].player_id, hider_id)
    engine.select_first_player(players[0].player_id, first_player_id)

    assert engine.state.selected_trump_hider_id == hider_id
    assert engine.state.trump_state.trump_hider_id == hider_id
    assert engine.state.selected_first_player_id == first_player_id


@pytest.mark.parametrize("player_count", [6, 8])
def test_normal_mode_first_player_turn_assignment(engine, six_players, eight_players, player_count):
    players = six_players if player_count == 6 else eight_players
    engine.create_game(
        f"game_normal_{player_count}p",
        players,
        host_id=players[0].player_id,
        hidden_trump_mode=False
    )

    first_player_id = players[3].player_id
    engine.select_first_player(players[0].player_id, first_player_id)
    state = engine.deal_cards()

    assert state.phase == GamePhase.PLAYING
    assert state.current_turn == first_player_id


@pytest.mark.parametrize("player_count", [6, 8])
def test_hidden_trump_mode_flow_and_first_player(engine, six_players, eight_players, player_count):
    players = six_players if player_count == 6 else eight_players
    engine.create_game(
        f"game_hidden_{player_count}p",
        players,
        host_id=players[0].player_id,
        hidden_trump_mode=True
    )

    hider_id = players[1].player_id
    first_player_id = players[5].player_id

    engine.select_trump_hider(players[0].player_id, hider_id)
    engine.select_first_player(players[0].player_id, first_player_id)

    state = engine.deal_cards()
    assert state.phase == GamePhase.HIDDEN_TRUMP_SELECTION

    state = engine.select_hidden_card(hider_id, 0)
    assert state.phase == GamePhase.HIDDEN_TRUMP_REVEAL

    state = engine.complete_hidden_trump_setup()
    assert state.phase == GamePhase.PLAYING
    assert state.current_turn == first_player_id
