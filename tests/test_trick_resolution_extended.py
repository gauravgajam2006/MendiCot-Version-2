import pytest
from mendicot.engine import MendiCotEngine
from mendicot.enums import GamePhase, Rank, Suit, TrumpStatus
from mendicot.models import Card, PlayedCard, Player, Trick, TrumpState

def _played(player_id, suit, rank):
    return PlayedCard(player_id, Card(suit, rank))

def _set_trick(engine, played_cards, lead_suit):
    engine.state.current_trick = Trick("P1", lead_suit, played_cards)
    return engine.get_current_trick_leader()

# ==========================================
# 7. REQUIRED HIDDEN TRUMP TESTS
# ==========================================

# A. Hidden and unrevealed
def test_hidden_unrevealed_diamond_ace_vs_spades_5(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.HIDDEN, Suit.SPADES)
    leader = _set_trick(engine, [
        _played("P1", Suit.DIAMONDS, Rank.ACE),
        _played("P2", Suit.SPADES, Rank.FIVE),
    ], Suit.DIAMONDS)
    assert leader.player_id == "P1"

def test_hidden_unrevealed_diamond_3_vs_spades_ace(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.HIDDEN, Suit.SPADES)
    leader = _set_trick(engine, [
        _played("P1", Suit.DIAMONDS, Rank.THREE),
        _played("P2", Suit.SPADES, Rank.ACE),
    ], Suit.DIAMONDS)
    assert leader.player_id == "P1"

def test_hidden_unrevealed_heart_king_vs_off_suit_discards(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.HIDDEN, Suit.DIAMONDS)
    leader = _set_trick(engine, [
        _played("P1", Suit.HEARTS, Rank.KING),
        _played("P2", Suit.CLUBS, Rank.ACE),
        _played("P3", Suit.SPADES, Rank.QUEEN),
    ], Suit.HEARTS)
    assert leader.player_id == "P1"

def test_hidden_unrevealed_multiple_discards_never_replace_leader(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.HIDDEN, Suit.SPADES)
    leader = _set_trick(engine, [
        _played("P1", Suit.HEARTS, Rank.FOUR),
        _played("P2", Suit.CLUBS, Rank.ACE),
        _played("P3", Suit.SPADES, Rank.KING), # Secret trump
        _played("P4", Suit.DIAMONDS, Rank.TEN),
    ], Suit.HEARTS)
    assert leader.player_id == "P1"

def test_hidden_unrevealed_provisional_leader_equals_final_winner(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.HIDDEN, Suit.SPADES)
    cards = [
        _played("P1", Suit.DIAMONDS, Rank.ACE),
        _played("P2", Suit.SPADES, Rank.FIVE),
        _played("P3", Suit.CLUBS, Rank.EIGHT),
        _played("P4", Suit.DIAMONDS, Rank.NINE),
    ]
    leader = _set_trick(engine, cards, Suit.DIAMONDS)

    game_state_4p.phase = GamePhase.TRICK_RESOLUTION
    engine.resolve_trick()
    assert game_state_4p.completed_tricks[-1].winner_player_id == leader.player_id

# B. Revealed/Public
def test_hidden_revealed_diamond_ace_vs_spades_5(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.PUBLIC, Suit.SPADES)
    leader = _set_trick(engine, [
        _played("P1", Suit.DIAMONDS, Rank.ACE),
        _played("P2", Suit.SPADES, Rank.FIVE),
    ], Suit.DIAMONDS)
    assert leader.player_id == "P2"

def test_hidden_revealed_two_trump_cards(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.PUBLIC, Suit.SPADES)
    leader = _set_trick(engine, [
        _played("P1", Suit.DIAMONDS, Rank.ACE),
        _played("P2", Suit.SPADES, Rank.FIVE),
        _played("P3", Suit.SPADES, Rank.KING),
    ], Suit.DIAMONDS)
    assert leader.player_id == "P3"

def test_hidden_revealed_no_trump_played(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.PUBLIC, Suit.SPADES)
    leader = _set_trick(engine, [
        _played("P1", Suit.DIAMONDS, Rank.NINE),
        _played("P2", Suit.CLUBS, Rank.ACE),
        _played("P3", Suit.DIAMONDS, Rank.JACK),
    ], Suit.DIAMONDS)
    assert leader.player_id == "P3"

def test_hidden_revealed_off_suit_non_trump_ace_remains_discard(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.PUBLIC, Suit.SPADES)
    leader = _set_trick(engine, [
        _played("P1", Suit.HEARTS, Rank.KING),
        _played("P2", Suit.CLUBS, Rank.ACE),
    ], Suit.HEARTS)
    assert leader.player_id == "P1"

# C. Reveal during an active trick
def test_reveal_during_active_trick(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.HIDDEN, Suit.SPADES)

    # 1. Before reveal, secret trump card is treated as discard
    cards_before_reveal = [
        _played("P1", Suit.DIAMONDS, Rank.ACE),
        _played("P2", Suit.SPADES, Rank.FIVE),
    ]
    leader_before = _set_trick(engine, cards_before_reveal, Suit.DIAMONDS)
    assert leader_before.player_id == "P1"

    # 2. Status becomes PUBLIC
    engine.state.trump_state.status = TrumpStatus.PUBLIC
    leader_after = engine.get_current_trick_leader()

    # 3. Authoritative calculation follows rules consistently
    assert leader_after.player_id == "P2"

    # 4. Final resolution matches current-leader metadata
    game_state_4p.current_trick.played_cards.extend([
        _played("P3", Suit.CLUBS, Rank.NINE),
        _played("P4", Suit.DIAMONDS, Rank.THREE),
    ])
    game_state_4p.phase = GamePhase.TRICK_RESOLUTION
    engine.resolve_trick()
    assert game_state_4p.completed_tricks[-1].winner_player_id == leader_after.player_id


# D. 4/6/8 player modes (Hidden)
@pytest.mark.parametrize("player_count", [4, 6, 8])
def test_hidden_trump_all_modes(player_count):
    players = [Player(f"P{i}", "TeamA" if i % 2 else "TeamB", i - 1, f"Player {i}") for i in range(1, player_count + 1)]
    engine = MendiCotEngine()
    engine.create_game("game", players, "P1", hidden_trump_mode=True)

    # Unrevealed secret trump inactive
    engine.state.trump_state = TrumpState(TrumpStatus.HIDDEN, Suit.SPADES)
    cards = [_played(f"P{i}", Suit.HEARTS, Rank(3 + i)) if i != player_count else _played(f"P{i}", Suit.SPADES, Rank.KING) for i in range(1, player_count + 1)]
    leader = _set_trick(engine, cards, Suit.HEARTS)
    assert leader.player_id == f"P{player_count - 1}" # Highest heart wins

    # Public trump active
    engine.state.trump_state.status = TrumpStatus.PUBLIC
    leader_public = engine.get_current_trick_leader()
    assert leader_public.player_id == f"P{player_count}" # Spade king wins


# ==========================================
# 8. REQUIRED NORMAL TRUMP TESTS
# ==========================================

def test_normal_trump_cuts_lead_suit(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.PUBLIC, Suit.SPADES)
    leader = _set_trick(engine, [
        _played("P1", Suit.HEARTS, Rank.ACE),
        _played("P2", Suit.SPADES, Rank.THREE),
    ], Suit.HEARTS)
    assert leader.player_id == "P2"

def test_normal_trump_highest_wins(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.PUBLIC, Suit.SPADES)
    leader = _set_trick(engine, [
        _played("P1", Suit.HEARTS, Rank.ACE),
        _played("P2", Suit.SPADES, Rank.THREE),
        _played("P3", Suit.SPADES, Rank.KING),
    ], Suit.HEARTS)
    assert leader.player_id == "P3"

def test_normal_trump_off_suit_is_discard(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.PUBLIC, Suit.SPADES)
    leader = _set_trick(engine, [
        _played("P1", Suit.HEARTS, Rank.KING),
        _played("P2", Suit.CLUBS, Rank.ACE),
    ], Suit.HEARTS)
    assert leader.player_id == "P1"

def test_normal_trump_highest_lead_suit_wins_no_trump(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.PUBLIC, Suit.SPADES)
    leader = _set_trick(engine, [
        _played("P1", Suit.HEARTS, Rank.EIGHT),
        _played("P2", Suit.HEARTS, Rank.KING),
        _played("P3", Suit.CLUBS, Rank.ACE),
    ], Suit.HEARTS)
    assert leader.player_id == "P2"

def test_normal_trump_provisional_equals_final_winner(game_state_4p, engine):
    engine.state.trump_state = TrumpState(TrumpStatus.PUBLIC, Suit.SPADES)
    cards = [
        _played("P1", Suit.HEARTS, Rank.ACE),
        _played("P2", Suit.SPADES, Rank.THREE),
        _played("P3", Suit.CLUBS, Rank.EIGHT),
        _played("P4", Suit.HEARTS, Rank.NINE),
    ]
    leader = _set_trick(engine, cards, Suit.HEARTS)

    game_state_4p.phase = GamePhase.TRICK_RESOLUTION
    engine.resolve_trick()
    assert game_state_4p.completed_tricks[-1].winner_player_id == leader.player_id

@pytest.mark.parametrize("player_count", [4, 6, 8])
def test_normal_trump_all_modes(player_count):
    players = [Player(f"P{i}", "TeamA" if i % 2 else "TeamB", i - 1, f"Player {i}") for i in range(1, player_count + 1)]
    engine = MendiCotEngine()
    engine.create_game("game", players, "P1", hidden_trump_mode=False)

    engine.state.trump_state = TrumpState(TrumpStatus.PUBLIC, Suit.SPADES)
    cards = [_played(f"P{i}", Suit.HEARTS, Rank(3 + i)) if i != player_count else _played(f"P{i}", Suit.SPADES, Rank.KING) for i in range(1, player_count + 1)]
    leader = _set_trick(engine, cards, Suit.HEARTS)
    assert leader.player_id == f"P{player_count}"
