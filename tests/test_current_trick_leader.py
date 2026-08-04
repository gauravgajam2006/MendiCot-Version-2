import pytest

from mendicot.engine import MendiCotEngine
from mendicot.enums import GamePhase, Rank, Suit, TrumpStatus
from mendicot.models import Card, PlayedCard, Player, Trick, TrumpState


def _played(player_id, suit, rank):
    return PlayedCard(player_id, Card(suit, rank))


def _set_trick(engine, played_cards, lead_suit=Suit.HEARTS):
    engine.state.current_trick = Trick("P1", lead_suit, played_cards)
    return engine.get_current_trick_leader()


def test_no_played_card_has_no_current_leader(game_state_4p, engine):
    assert engine.get_current_trick_leader() is None
    assert engine.get_player_view("P1")["current_trick_leader"] is None


def test_first_played_card_becomes_current_leader(game_state_4p, engine):
    leader = _set_trick(engine, [_played("P1", Suit.HEARTS, Rank.SEVEN)])
    assert leader == _played("P1", Suit.HEARTS, Rank.SEVEN)


def test_higher_lead_suit_card_replaces_current_leader(game_state_4p, engine):
    leader = _set_trick(engine, [
        _played("P1", Suit.HEARTS, Rank.SEVEN),
        _played("P2", Suit.HEARTS, Rank.QUEEN),
    ])
    assert leader.player_id == "P2"
    assert leader.card == Card(Suit.HEARTS, Rank.QUEEN)


def test_off_suit_non_trump_does_not_replace_current_leader(game_state_4p, engine):
    leader = _set_trick(engine, [
        _played("P1", Suit.HEARTS, Rank.SEVEN),
        _played("P2", Suit.CLUBS, Rank.ACE),
    ])
    assert leader.player_id == "P1"


def test_trump_replaces_lead_suit_card(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.PUBLIC, Suit.SPADES)
    leader = _set_trick(engine, [
        _played("P1", Suit.HEARTS, Rank.ACE),
        _played("P2", Suit.SPADES, Rank.THREE),
    ])
    assert leader.player_id == "P2"


def test_higher_trump_replaces_lower_trump(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.PUBLIC, Suit.SPADES)
    leader = _set_trick(engine, [
        _played("P1", Suit.HEARTS, Rank.ACE),
        _played("P2", Suit.SPADES, Rank.THREE),
        _played("P3", Suit.SPADES, Rank.KING),
    ])
    assert leader.player_id == "P3"


def test_hidden_trump_inactive_until_revealed(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.HIDDEN, Suit.SPADES)
    leader = _set_trick(engine, [
        _played("P1", Suit.HEARTS, Rank.ACE),
        _played("P2", Suit.SPADES, Rank.THREE),
    ])
    assert leader.player_id == "P1"


def test_provisional_leader_matches_final_winner_and_new_trick_resets(
    game_state_4p, engine
):
    cards = [
        _played("P1", Suit.HEARTS, Rank.SEVEN),
        _played("P2", Suit.HEARTS, Rank.QUEEN),
        _played("P3", Suit.CLUBS, Rank.ACE),
        _played("P4", Suit.HEARTS, Rank.KING),
    ]
    provisional_id = _set_trick(engine, cards).player_id
    game_state_4p.phase = GamePhase.TRICK_RESOLUTION

    engine.resolve_trick()

    assert game_state_4p.completed_tricks[-1].winner_player_id == provisional_id
    assert engine.get_current_trick_leader() is None
    assert engine.get_player_view("P1")["current_trick_leader"] is None


@pytest.mark.parametrize("player_count", [4, 6, 8])
def test_current_leader_supports_all_player_counts(player_count):
    players = [
        Player(f"P{i}", "TeamA" if i % 2 else "TeamB", i - 1, f"Player {i}")
        for i in range(1, player_count + 1)
    ]
    engine = MendiCotEngine()
    engine.create_game("game", players, "P1")
    cards = [
        _played(player.player_id, Suit.HEARTS, Rank(3 + player.seat_index))
        for player in players
    ]

    leader = _set_trick(engine, cards)

    assert leader.player_id == f"P{player_count}"


def test_player_view_serializes_public_leader_identically_for_every_player():
    players = [
        Player("P1", "TeamA", 0, "JD"),
        Player("P2", "TeamB", 1, "AG"),
        Player("P3", "TeamA", 2, "MK"),
        Player("P4", "TeamB", 3, "RS"),
    ]
    engine = MendiCotEngine()
    engine.create_game("game", players, "P1")
    _set_trick(engine, [
        _played("P1", Suit.HEARTS, Rank.SEVEN),
        _played("P2", Suit.HEARTS, Rank.QUEEN),
    ])
    expected = {
        "player_id": "P2",
        "display_name": "AG",
        "card": {"suit": Suit.HEARTS, "rank": Rank.QUEEN},
    }

    assert all(
        engine.get_player_view(player.player_id)["current_trick_leader"] == expected
        for player in players
    )
    assert all(
        list(engine.get_player_view(player.player_id)["hands"]) == [player.player_id]
        for player in players
    )
