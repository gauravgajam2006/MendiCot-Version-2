"""Integration tests for complete gameplay in 6-player and 8-player MendiCot games.

Covers: trick play, trick resolution, scoring, follow-suit enforcement,
trump behavior (normal and hidden), and full game completion.
"""

import pytest
from mendicot.enums import GamePhase, TrumpStatus, Suit, Rank
from mendicot.models import Card, Player
from mendicot.exceptions import MustFollowSuit
from mendicot.validators import validate_follow_suit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_valid_card(hand, trick, trump_state):
    """Find the first valid card to play, respecting follow-suit rules.

    Same logic used by test_full_game_4_players in test_scoring.py.
    """
    from mendicot.exceptions import MustPlayTrump

    if not trick.played_cards:
        return hand[0]

    for card in hand:
        try:
            validate_follow_suit(hand, trick.lead_suit, card, trump_state)
            return card
        except (MustFollowSuit, MustPlayTrump):
            continue
    # Should never reach here if engine rules are consistent
    return hand[0]  # pragma: no cover


def _get_players(six_players, eight_players, player_count):
    """Select the right fixture based on player_count."""
    return six_players if player_count == 6 else eight_players


def _setup_normal_game(engine, players):
    """Create and deal a normal-mode game, return state."""
    engine.create_game(
        "gameplay_test",
        players,
        host_id=players[0].player_id,
        hidden_trump_mode=False,
    )
    return engine.deal_cards()


def _assign_controlled_hands(state, player_count):
    """Give every player cards of one suit so follow-suit is predictable.

    6p (8 cards each): P1-P4 get 2 suits split, P5-P6 get remaining.
    8p (6 cards each): Each player gets 6 cards from assigned suits.

    Actually, for controlled single-trick tests we just need all players to
    hold cards of the SAME suit so everyone can follow suit.
    """
    # All players get HEARTS — simplest deterministic setup for a single trick.
    cards_per_player = 48 // player_count
    all_ranks = list(Rank)  # 3..14, 12 ranks

    seat_order = state.seat_order
    for i, pid in enumerate(seat_order):
        start = i * cards_per_player
        # Assign hearts with distinct ranks, wrapping across suits if needed
        hand = []
        for j in range(cards_per_player):
            idx = start + j
            suit = list(Suit)[idx // len(all_ranks)]
            rank = all_ranks[idx % len(all_ranks)]
            hand.append(Card(suit, rank))
        state.hands[pid] = hand


# ---------------------------------------------------------------------------
# 1. Single-trick play & resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("player_count", [6, 8])
def test_complete_trick_played_by_all_players(
    engine, six_players, eight_players, player_count
):
    players = _get_players(six_players, eight_players, player_count)
    state = _setup_normal_game(engine, players)

    # Give all players HEARTS so everyone can follow suit
    for pid in state.seat_order:
        state.hands[pid] = [Card(Suit.HEARTS, rank) for rank in list(Rank)[:48 // player_count]]

    leader = state.current_turn
    for _ in range(player_count):
        pid = state.current_turn
        card = _find_valid_card(state.hands[pid], state.current_trick, state.trump_state)
        engine.play_card(pid, card)

    assert state.phase == GamePhase.TRICK_RESOLUTION


@pytest.mark.parametrize("player_count", [6, 8])
def test_resolve_trick_determines_winner(
    engine, six_players, eight_players, player_count
):
    players = _get_players(six_players, eight_players, player_count)
    state = _setup_normal_game(engine, players)

    ranks = list(Rank)  # [THREE(3) .. ACE(14)], 12 ranks

    # Give each player exactly 1 HEARTS card with ascending ranks.
    # P1 gets lowest rank, last player gets highest → last player wins.
    for i, pid in enumerate(state.seat_order):
        state.hands[pid] = [Card(Suit.HEARTS, ranks[i])]

    state.current_turn = state.seat_order[0]

    for _ in range(player_count):
        pid = state.current_turn
        engine.play_card(pid, state.hands[pid][0])

    state = engine.resolve_trick()

    last_player_id = state.seat_order[player_count - 1]
    assert state.completed_tricks[-1].winner_player_id == last_player_id


@pytest.mark.parametrize("player_count", [6, 8])
def test_winning_team_receives_captured_cards(
    engine, six_players, eight_players, player_count
):
    players = _get_players(six_players, eight_players, player_count)
    state = _setup_normal_game(engine, players)

    # All HEARTS, P1 has highest card (ACE)
    ranks = list(Rank)
    for i, pid in enumerate(state.seat_order):
        state.hands[pid] = [Card(Suit.HEARTS, ranks[i])]

    # Ensure P1 leads
    state.current_turn = "P1"

    for _ in range(player_count):
        pid = state.current_turn
        engine.play_card(pid, state.hands[pid][0])

    # P1 has ACE (rank index 11 = Rank.ACE=14... let me check: ranks[0]=THREE, last player gets highest)
    # Actually ranks = [3,4,5,6,7,8,9,10,11,12,13,14], P1 gets ranks[0]=THREE
    # Highest would be the last player. Let me just resolve and check the winner's team.
    engine.resolve_trick()

    # Find which team won
    winner_id = state.completed_tricks[-1].winner_player_id
    winner_team = next(p.team_id for p in state.players if p.player_id == winner_id)

    assert len(state.teams[winner_team].captured_cards) == player_count


@pytest.mark.parametrize("player_count", [6, 8])
def test_winning_team_tricks_won_increments(
    engine, six_players, eight_players, player_count
):
    players = _get_players(six_players, eight_players, player_count)
    state = _setup_normal_game(engine, players)

    for pid in state.seat_order:
        state.hands[pid] = [Card(Suit.HEARTS, Rank.THREE + state.seat_order.index(pid))]

    state.current_turn = state.seat_order[0]
    for _ in range(player_count):
        pid = state.current_turn
        engine.play_card(pid, state.hands[pid][0])

    engine.resolve_trick()

    winner_id = state.completed_tricks[-1].winner_player_id
    winner_team = next(p.team_id for p in state.players if p.player_id == winner_id)

    assert state.teams[winner_team].tricks_won == 1


@pytest.mark.parametrize("player_count", [6, 8])
def test_tens_captured_counted_correctly(
    engine, six_players, eight_players, player_count
):
    players = _get_players(six_players, eight_players, player_count)
    state = _setup_normal_game(engine, players)

    # Give each player one HEARTS card; include a TEN for one of them
    ranks = list(Rank)
    for i, pid in enumerate(state.seat_order):
        state.hands[pid] = [Card(Suit.HEARTS, ranks[i])]

    # ranks[7] = TEN (Rank values: 3,4,5,6,7,8,9,10,11,12,13,14 → index 7 = 10)
    # So the player at seat index 7 (8p) or no player (6p) gets a TEN.
    # Let's just explicitly set one player's card to TEN and another to ACE (winner).
    state.hands[state.seat_order[0]] = [Card(Suit.HEARTS, Rank.TEN)]
    state.hands[state.seat_order[-1]] = [Card(Suit.HEARTS, Rank.ACE)]
    # Fill remaining with non-ten ranks
    non_ten_ranks = [r for r in Rank if r not in (Rank.TEN, Rank.ACE)]
    for i, pid in enumerate(state.seat_order[1:-1]):
        state.hands[pid] = [Card(Suit.HEARTS, non_ten_ranks[i])]

    state.current_turn = state.seat_order[0]
    for _ in range(player_count):
        pid = state.current_turn
        engine.play_card(pid, state.hands[pid][0])

    engine.resolve_trick()

    winner_id = state.completed_tricks[-1].winner_player_id
    winner_team = next(p.team_id for p in state.players if p.player_id == winner_id)

    assert state.teams[winner_team].tens_captured == 1


@pytest.mark.parametrize("player_count", [6, 8])
def test_trick_winner_becomes_next_turn(
    engine, six_players, eight_players, player_count
):
    players = _get_players(six_players, eight_players, player_count)
    state = _setup_normal_game(engine, players)

    ranks = list(Rank)  # 12 ranks
    suits = [Suit.HEARTS, Suit.SPADES]

    # Give each player 2 cards (all same suit so follow-suit works).
    # Use HEARTS for the first 12 slots, SPADES overflow if needed.
    for i, pid in enumerate(state.seat_order):
        cards = []
        for j in range(2):
            idx = i * 2 + j
            suit = suits[idx // len(ranks)]
            rank = ranks[idx % len(ranks)]
            cards.append(Card(suit, rank))
        state.hands[pid] = cards

    # All first cards are HEARTS (indices 0-11 for up to 6 players).
    # For 8 players, players 7+ get SPADES first cards — they'll be on cut.
    # That's fine; we just need the trick to complete and resolve.
    state.current_turn = state.seat_order[0]
    for _ in range(player_count):
        pid = state.current_turn
        engine.play_card(pid, state.hands[pid][0])

    engine.resolve_trick()

    winner_id = state.completed_tricks[-1].winner_player_id
    assert state.current_turn == winner_id
    assert state.phase == GamePhase.PLAYING


# ---------------------------------------------------------------------------
# 2. Follow-suit enforcement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("player_count", [6, 8])
def test_must_follow_lead_suit(
    engine, six_players, eight_players, player_count
):
    players = _get_players(six_players, eight_players, player_count)
    state = _setup_normal_game(engine, players)

    leader = state.current_turn
    next_pid = state.seat_order[(state.seat_order.index(leader) + 1) % player_count]

    # Leader has HEARTS, next player has both HEARTS and SPADES
    state.hands[leader] = [Card(Suit.HEARTS, Rank.ACE)]
    state.hands[next_pid] = [Card(Suit.HEARTS, Rank.THREE), Card(Suit.SPADES, Rank.KING)]

    engine.play_card(leader, Card(Suit.HEARTS, Rank.ACE))

    with pytest.raises(MustFollowSuit):
        engine.play_card(next_pid, Card(Suit.SPADES, Rank.KING))


@pytest.mark.parametrize("player_count", [6, 8])
def test_can_play_any_suit_when_on_cut(
    engine, six_players, eight_players, player_count
):
    players = _get_players(six_players, eight_players, player_count)
    state = _setup_normal_game(engine, players)

    leader = state.current_turn
    next_pid = state.seat_order[(state.seat_order.index(leader) + 1) % player_count]

    # Leader has HEARTS, next player has NO HEARTS (on cut)
    state.hands[leader] = [Card(Suit.HEARTS, Rank.ACE)]
    state.hands[next_pid] = [Card(Suit.SPADES, Rank.KING), Card(Suit.CLUBS, Rank.FIVE)]

    engine.play_card(leader, Card(Suit.HEARTS, Rank.ACE))
    # Should not raise — player is on cut
    engine.play_card(next_pid, Card(Suit.SPADES, Rank.KING))


# ---------------------------------------------------------------------------
# 3. Trump behavior
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("player_count", [6, 8])
def test_normal_trump_first_cut_sets_trump(
    engine, six_players, eight_players, player_count
):
    players = _get_players(six_players, eight_players, player_count)
    state = _setup_normal_game(engine, players)

    leader = state.current_turn
    next_pid = state.seat_order[(state.seat_order.index(leader) + 1) % player_count]

    state.hands[leader] = [Card(Suit.HEARTS, Rank.ACE)]
    state.hands[next_pid] = [Card(Suit.SPADES, Rank.KING)]  # no HEARTS → on cut

    engine.play_card(leader, Card(Suit.HEARTS, Rank.ACE))
    engine.play_card(next_pid, Card(Suit.SPADES, Rank.KING))

    assert state.trump_state.suit == Suit.SPADES
    assert state.trump_state.status == TrumpStatus.PUBLIC


@pytest.mark.parametrize("player_count", [6, 8])
def test_hidden_trump_auto_reveals_on_cut_with_trump(
    engine, six_players, eight_players, player_count
):
    players = _get_players(six_players, eight_players, player_count)

    engine.create_game(
        "hidden_gameplay_test",
        players,
        host_id=players[0].player_id,
        hidden_trump_mode=True,
    )

    hider = players[0]
    engine.select_trump_hider(hider.player_id, hider.player_id)
    state = engine.deal_cards()

    # Force hider's hand to CLUBS so hidden trump becomes CLUBS
    state.hands[hider.player_id] = [
        Card(Suit.CLUBS, rank) for rank in list(Rank)[:48 // player_count]
    ]
    engine.select_hidden_card(hider.player_id, 0)
    engine.complete_hidden_trump_setup()

    assert state.trump_state.status == TrumpStatus.HIDDEN

    # First player leads HEARTS
    leader = state.current_turn
    next_pid = state.seat_order[(state.seat_order.index(leader) + 1) % player_count]

    state.hands[leader] = [Card(Suit.HEARTS, Rank.ACE)]
    # Next player has no HEARTS but has CLUBS (trump) → auto-reveal
    state.hands[next_pid] = [Card(Suit.CLUBS, Rank.KING)]

    engine.play_card(leader, Card(Suit.HEARTS, Rank.ACE))
    engine.play_card(next_pid, Card(Suit.CLUBS, Rank.KING))

    assert state.trump_state.status == TrumpStatus.PUBLIC


# ---------------------------------------------------------------------------
# 4. Full game completion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "player_count,expected_tricks",
    [(6, 8), (8, 6)],
)
def test_full_game_reaches_game_over_or_draw(
    engine, six_players, eight_players, player_count, expected_tricks
):
    players = _get_players(six_players, eight_players, player_count)
    state = _setup_normal_game(engine, players)

    while state.phase in (GamePhase.PLAYING, GamePhase.TRICK_RESOLUTION):
        if state.phase == GamePhase.TRICK_RESOLUTION:
            engine.resolve_trick()
        else:
            pid = state.current_turn
            hand = state.hands[pid]
            card = _find_valid_card(hand, state.current_trick, state.trump_state)
            engine.play_card(pid, card)

    assert state.phase in (GamePhase.GAME_OVER, GamePhase.DRAW)

    total_tricks = (
        state.teams["TeamA"].tricks_won + state.teams["TeamB"].tricks_won
    )
    assert total_tricks == expected_tricks

    total_tens = (
        state.teams["TeamA"].tens_captured + state.teams["TeamB"].tens_captured
    )
    assert total_tens == 4
