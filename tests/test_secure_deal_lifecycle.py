from concurrent.futures import ThreadPoolExecutor
import logging
import threading

import pytest

from mendicot.deck import DEALING_ALGORITHM_VERSION, DECK_DEFINITION_VERSION
from mendicot.engine import MendiCotEngine
from mendicot.enums import GamePhase, TrumpStatus
from mendicot.exceptions import (
    DealAlreadyCompleted,
    InvalidPhase,
    InvalidSeatConfiguration,
    ShuffleVerificationFailed,
)
from mendicot.models import Card, Player, PlayedCard, Trick
from mendicot.room import GameRoom, RoomStatus
from mendicot.secure_shuffle import (
    DeterministicEntropySource,
    SHUFFLE_ALGORITHM_VERSION,
)



def _players(player_count: int) -> list[Player]:
    return [
        Player(
            player_id=f"P{seat_index}",
            team_id="TeamA" if seat_index % 2 == 0 else "TeamB",
            seat_index=seat_index,
        )
        for seat_index in range(player_count)
    ]


def _dealt_engine(
    player_count: int = 4,
    *,
    hidden: bool = False,
    seed: bytes = b"secure-deal-lifecycle-seed",
) -> MendiCotEngine:
    players = _players(player_count)
    engine = MendiCotEngine(DeterministicEntropySource(seed))
    engine.create_game(
        game_id=f"game-{player_count}-{'hidden' if hidden else 'normal'}",
        room_id="ROOM-AUDIT",
        players=list(reversed(players)),
        host_id="P0",
        hidden_trump_mode=hidden,
    )
    if hidden:
        engine.select_trump_hider("P0", "P0")
    engine.deal_cards()
    return engine


@pytest.mark.parametrize(
    ("player_count", "expected_hand_size"),
    [(4, 12), (6, 8), (8, 6)],
)
@pytest.mark.parametrize("hidden", [False, True])
def test_all_modes_use_one_secure_pipeline_and_deal_all_cards(
    player_count: int,
    expected_hand_size: int,
    hidden: bool,
) -> None:
    engine = _dealt_engine(player_count, hidden=hidden)
    state = engine.state

    assert state.seat_order == [f"P{i}" for i in range(player_count)]
    assert {len(hand) for hand in state.hands.values()} == {expected_hand_size}
    all_cards = [card for hand in state.hands.values() for card in hand]
    assert len(all_cards) == 48
    assert len(set(all_cards)) == 48
    assert state.phase == (
        GamePhase.HIDDEN_TRUMP_SELECTION if hidden else GamePhase.PLAYING
    )
    assert state.deck_definition_version == DECK_DEFINITION_VERSION
    assert state.shuffle_algorithm_version == SHUFFLE_ALGORITHM_VERSION
    assert state.dealing_algorithm_version == DEALING_ALGORITHM_VERSION
    assert engine.verify_deal_audit() is True
    assert engine._deal_audit.context["trump_mode"] == (
        "hidden" if hidden else "normal"
    )


def test_scrambled_input_is_sorted_by_seat_and_dealt_round_robin() -> None:
    engine = _dealt_engine(4)
    state = engine.state
    shuffled_deck = engine._deal_audit.shuffled_deck

    assert state.seat_order == ["P0", "P1", "P2", "P3"]
    for seat_index, player_id in enumerate(state.seat_order):
        assert state.hands[player_id] == list(shuffled_deck[seat_index::4])


@pytest.mark.parametrize(
    "players",
    [
        [
            Player("P0", "TeamA", 0),
            Player("P1", "TeamB", 1),
            Player("P2", "TeamA", 1),
            Player("P3", "TeamB", 3),
        ],
        [
            Player("P0", "TeamA", 0),
            Player("P1", "TeamB", 1),
            Player("P2", "TeamA", 2),
            Player("P3", "TeamB", 4),
        ],
        [
            Player("P0", "TeamA", 0),
            Player("P1", "TeamB", 1),
            Player("P0", "TeamA", 2),
            Player("P3", "TeamB", 3),
        ],
    ],
    ids=["duplicate-seat", "missing-seat", "duplicate-player-id"],
)
def test_invalid_authoritative_seat_configuration_is_rejected(
    players: list[Player],
) -> None:
    engine = MendiCotEngine(DeterministicEntropySource(b"invalid-seats"))

    with pytest.raises(InvalidSeatConfiguration):
        engine.create_game("invalid", players, host_id=players[0].player_id)


def test_duplicate_deal_does_not_overwrite_committed_state() -> None:
    engine = _dealt_engine(4)
    original_hands = {
        player_id: tuple(hand) for player_id, hand in engine.state.hands.items()
    }
    original_commitment = engine.state.shuffle_commitment
    original_generation = engine.state.deal_generation
    original_version = engine.state.version
    original_audit = engine._deal_audit

    with pytest.raises(DealAlreadyCompleted):
        engine.deal_cards()

    assert {
        player_id: tuple(hand) for player_id, hand in engine.state.hands.items()
    } == original_hands
    assert engine.state.shuffle_commitment == original_commitment
    assert engine.state.deal_generation == original_generation == 1
    assert engine.state.version == original_version
    assert engine._deal_audit is original_audit


def test_concurrent_room_setup_requests_commit_exactly_one_deal() -> None:
    room = GameRoom("concurrent-deal", configured_player_count=4)
    for player in _players(4):
        room.add_player(player.player_id)
    room.start_game("P0", set(room.player_ids))
    barrier = threading.Barrier(2)

    def select_and_deal() -> object:
        barrier.wait()
        try:
            room.select_first_player("P0", "P0")
            return "committed"
        except Exception as error:  # Assert the exact losing request below.
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(select_and_deal) for _ in range(2)]
        outcomes = [future.result(timeout=10) for future in futures]

    assert outcomes.count("committed") == 1
    failures = [outcome for outcome in outcomes if outcome != "committed"]
    assert len(failures) == 1
    assert isinstance(failures[0], InvalidPhase)
    assert room.status == RoomStatus.IN_GAME
    assert room.engine.state.dealt is True
    assert room.engine.state.deal_generation == 1
    assert sum(len(hand) for hand in room.engine.state.hands.values()) == 48


def test_committed_configuration_and_version_metadata_are_frozen_in_audit() -> None:
    players = _players(4)
    engine = MendiCotEngine(DeterministicEntropySource(b"committed-context"))
    engine.create_game(
        "GAME-42",
        list(reversed(players)),
        host_id="P0",
        hidden_trump_mode=True,
        room_id="ROOM-42",
    )
    engine.select_first_player("P0", "P2")
    engine.select_trump_hider("P0", "P1")
    version_before_deal = engine.state.version
    engine.deal_cards()

    state = engine.state
    context = engine._deal_audit.context
    assert state.configuration_locked is True
    assert state.dealt is True
    assert state.deal_generation == 1
    assert state.version == version_before_deal + 1
    assert context["game_id"] == "GAME-42"
    assert context["room_id"] == "ROOM-42"
    assert context["player_count"] == 4
    assert context["trump_mode"] == "hidden"
    assert context["selected_first_player_id"] == "P2"
    assert context["selected_trump_hider_id"] == "P1"
    assert [seat["player_id"] for seat in context["ordered_seats"]] == state.seat_order
    assert context["deck_definition_version"] == DECK_DEFINITION_VERSION
    assert context["shuffle_algorithm_version"] == SHUFFLE_ALGORITHM_VERSION
    assert context["dealing_algorithm_version"] == DEALING_ALGORITHM_VERSION
    assert state.shuffle_commitment == engine.get_shuffle_audit()["commitment_hash"]


def test_audit_secret_is_private_during_play_and_available_at_terminal_phase() -> None:
    engine = _dealt_engine(4)
    active_audit = engine.get_player_view("P0")["shuffle_audit"]
    private_keys = {
        "revealed_server_secret",
        "nonce",
        "canonical_context",
        "shuffled_deck_hash",
        "shuffled_deck",
        "deal_timestamp",
    }

    assert private_keys.isdisjoint(active_audit)
    with pytest.raises(InvalidPhase):
        engine.get_shuffle_audit(reveal=True)
    assert engine.verify_deal_audit() is True



@pytest.mark.parametrize("hidden", [False, True])
def test_player_views_never_include_opponent_hands(hidden: bool) -> None:
    engine = _dealt_engine(4, hidden=hidden)

    for player_id in engine.state.seat_order:
        view = engine.get_player_view(player_id)
        assert set(view["hands"]) == {player_id}
        if hidden and player_id == "P0":
            assert view["hands"][player_id] == []
        else:
            assert view["hands"][player_id] == [
                {"suit": card.suit, "rank": card.rank}
                for card in engine.state.hands[player_id]
            ]


def test_hidden_hider_receives_blind_positions_without_card_identities() -> None:
    engine = _dealt_engine(6, hidden=True)
    view = engine.get_player_view("P0")

    assert view["phase"] == GamePhase.HIDDEN_TRUMP_SELECTION
    assert view["hands"] == {"P0": []}
    assert view["hidden_hand_positions"] == list(range(8))
    assert view["trump_state"]["suit"] is None
    assert view["trump_state"]["hidden_rank"] is None
    assert view["trump_state"]["hidden_card_index"] is None
    assert "shuffled_deck" not in view["shuffle_audit"]


def test_reconnect_style_views_do_not_generate_or_replace_a_deal() -> None:
    engine = _dealt_engine(8, hidden=True)
    hands_before = {
        player_id: tuple(hand) for player_id, hand in engine.state.hands.items()
    }
    commitment_before = engine.state.shuffle_commitment
    audit_before = engine._deal_audit

    first_view = engine.get_player_view("P3")
    replacement_view = engine.get_player_view("P3")

    assert first_view["hands"] == replacement_view["hands"]
    assert first_view["shuffle_commitment"] == commitment_before
    assert replacement_view["shuffle_commitment"] == commitment_before
    assert engine.state.deal_generation == 1
    assert engine._deal_audit is audit_before
    assert {
        player_id: tuple(hand) for player_id, hand in engine.state.hands.items()
    } == hands_before


def test_post_deal_setup_inputs_cannot_mutate_committed_context() -> None:
    engine = _dealt_engine(4, hidden=True)
    context_before = dict(engine._deal_audit.context)
    version_before = engine.state.version

    with pytest.raises(InvalidPhase):
        engine.select_trump_hider("P0", "P2")
    with pytest.raises(InvalidPhase):
        engine.select_first_player("P0", "P2")

    assert engine._deal_audit.context == context_before
    assert engine.state.version == version_before
    assert engine.state.selected_trump_hider_id == "P0"


def test_public_hidden_trump_reveals_only_suit_not_card_identity() -> None:
    engine = _dealt_engine(4, hidden=True)
    engine.select_hidden_card("P0", 0)
    engine.complete_hidden_trump_setup()
    engine.state.trump_state.status = TrumpStatus.PUBLIC

    view = engine.get_player_view("P1")
    assert view["trump_state"]["suit"] is not None
    assert view["trump_state"]["hidden_rank"] is None
    assert view["trump_state"]["hidden_card_index"] is None

def test_stale_cancelled_setup_engine_cannot_replace_a_new_game() -> None:
    room = GameRoom("stale-setup", configured_player_count=4)
    for player in _players(4):
        room.add_player(player.player_id)
    online = set(room.player_ids)
    room.start_game("P0", online)
    stale_engine = room.engine
    room.cancel_game_setup("P0")
    room.start_game("P0", online)
    current_engine = room.engine

    stale_engine.select_first_player("P0", "P0")
    stale_engine.deal_cards()

    assert room.engine is current_engine
    assert current_engine is not stale_engine
    assert current_engine.state.dealt is False
    assert current_engine.state.deal_generation == 0
    assert current_engine.state.phase == GamePhase.FIRST_PLAYER_SELECTION


def test_secure_deal_logs_only_safe_audit_metadata(caplog) -> None:
    engine = MendiCotEngine(DeterministicEntropySource(b"safe-log-seed"))
    engine.create_game("safe-log", _players(4), host_id="P0", room_id="ROOM-LOG")
    caplog.set_level(logging.INFO, logger="mendicot.engine")
    engine.deal_cards()

    committed = next(
        record for record in caplog.records
        if record.getMessage() == "secure deal committed"
    )
    assert committed.game_id == "safe-log"
    assert committed.room_id == "ROOM-LOG"
    assert committed.commitment_hash == engine._deal_audit.commitment_hash
    assert committed.deck_hash == engine._deal_audit.shuffled_deck_hash
    assert committed.player_count == 4
    serialized_log = repr(committed.__dict__)
    assert engine._deal_audit.server_secret.hex() not in serialized_log
    assert "dealt_hands" not in committed.__dict__
    assert "shuffled_deck" not in committed.__dict__

    with pytest.raises(DealAlreadyCompleted):
        engine.deal_cards()
    duplicate = next(
        record for record in caplog.records
        if record.getMessage() == "duplicate deal rejected"
    )
    assert duplicate.error_category == "DEAL_ALREADY_COMPLETED"


# ---------------------------------------------------------------------------
# Terminal played-card ownership verification helpers
# ---------------------------------------------------------------------------

def _play_full_game(
    engine: MendiCotEngine,
    *,
    hidden: bool = False,
) -> None:
    """Play all cards through to FINAL_SCORE_DISPLAY (no finalize_game).

    Follows the full MendiCot rule set: follow lead suit, play trump when
    on cut with public trump, and handle hidden-trump auto-reveal.
    """
    state = engine.state

    if hidden:
        hider_id = state.trump_state.trump_hider_id
        engine.select_hidden_card(hider_id, 0)
        engine.complete_hidden_trump_setup(hider_id)

    tricks_needed = 48 // state.player_count

    for _ in range(tricks_needed):
        if state.phase == GamePhase.FINAL_SCORE_DISPLAY:
            break
        assert state.phase == GamePhase.PLAYING

        lead_id = state.current_turn
        lead_hand = state.hands[lead_id]
        lead_card = lead_hand[0]
        engine.play_card(lead_id, lead_card)
        lead_suit = lead_card.suit

        for _ in range(state.player_count - 1):
            pid = state.current_turn
            hand = state.hands[pid]

            # Follow lead suit if possible
            same_suit = [c for c in hand if c.suit == lead_suit]
            if same_suit:
                card = same_suit[0]
            else:
                # On cut: we must explicitly reveal hidden trump if holding trump.
                trump_st = state.trump_state
                is_hidden_trump_mode = state.hidden_trump_mode

                if (
                    is_hidden_trump_mode
                    and trump_st.status == TrumpStatus.HIDDEN
                    and trump_st.suit is not None
                ):
                    trumps = [c for c in hand if c.suit == trump_st.suit]
                    if trumps:
                        engine.reveal_trump(pid)
                        engine.complete_trump_reveal_display()
                        engine.complete_hidden_card_return()
                        card = trumps[0]
                    else:
                        card = hand[0]
                elif (
                    trump_st.suit is not None
                    and trump_st.status == TrumpStatus.PUBLIC
                ):
                    trumps = [c for c in hand if c.suit == trump_st.suit]
                    card = trumps[0] if trumps else hand[0]
                else:
                    card = hand[0]
            engine.play_card(pid, card)

        assert state.phase == GamePhase.TRICK_RESOLUTION
        engine.resolve_trick()

    assert state.phase == GamePhase.FINAL_SCORE_DISPLAY



# ---------------------------------------------------------------------------
# Terminal ownership verification — valid games
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("player_count", [4, 6, 8])
def test_finalize_game_marks_verified_after_valid_complete_game(
    player_count: int,
) -> None:
    engine = _dealt_engine(player_count, hidden=False)
    _play_full_game(engine)
    engine.finalize_game()
    assert engine._deal_audit.audit_status == "VERIFIED"
    assert engine.state.phase in (GamePhase.GAME_OVER, GamePhase.DRAW)


@pytest.mark.parametrize("player_count", [4, 6, 8])
def test_finalize_game_marks_verified_hidden_trump(
    player_count: int,
) -> None:
    engine = _dealt_engine(player_count, hidden=True)
    _play_full_game(engine, hidden=True)
    engine.finalize_game()
    assert engine._deal_audit.audit_status == "VERIFIED"
    assert engine.state.phase in (GamePhase.GAME_OVER, GamePhase.DRAW)


def test_terminal_audit_reveals_full_metadata_only_after_verified() -> None:
    engine = _dealt_engine(4)
    active_audit = engine.get_player_view("P0")["shuffle_audit"]
    private_keys = {
        "revealed_server_secret",
        "nonce",
        "canonical_context",
        "shuffled_deck_hash",
        "shuffled_deck",
        "deal_timestamp",
    }

    assert private_keys.isdisjoint(active_audit)
    with pytest.raises(InvalidPhase):
        engine.get_shuffle_audit(reveal=True)

    _play_full_game(engine)
    engine.finalize_game()
    terminal_audit = engine.get_player_view("P0")["shuffle_audit"]

    assert terminal_audit["audit_status"] == "VERIFIED"
    assert private_keys <= terminal_audit.keys()
    assert len(terminal_audit["revealed_server_secret"]) == 64
    assert len(terminal_audit["nonce"]) == 32
    assert len(terminal_audit["shuffled_deck"]) == 48
    assert engine.verify_deal_audit() is True


# ---------------------------------------------------------------------------
# Terminal ownership verification — injected failures
# ---------------------------------------------------------------------------

def _setup_tampered_game(player_count=4, hidden=False):
    """Return an engine at FINAL_SCORE_DISPLAY ready for tampered-history tests."""
    engine = _dealt_engine(player_count, hidden=hidden)
    _play_full_game(engine, hidden=hidden)
    assert engine.state.phase == GamePhase.FINAL_SCORE_DISPLAY
    return engine


def test_finalize_game_ownership_failed_wrong_owner() -> None:
    engine = _setup_tampered_game()
    state = engine.state
    # Swap player_id on the first played card of the first trick
    trick = state.completed_tricks[0]
    original_pc = trick.played_cards[0]
    wrong_pid = state.seat_order[1] if original_pc.player_id == state.seat_order[0] else state.seat_order[0]
    trick.played_cards[0] = PlayedCard(wrong_pid, original_pc.card)

    engine.finalize_game()
    assert engine._deal_audit.audit_status == "OWNERSHIP_FAILED"
    assert state.phase in (GamePhase.GAME_OVER, GamePhase.DRAW)


def test_finalize_game_ownership_failed_fabricated_card() -> None:
    engine = _setup_tampered_game()
    state = engine.state
    trick = state.completed_tricks[0]
    original_pc = trick.played_cards[0]
    fake_card = Card("JOKER", 99)
    trick.played_cards[0] = PlayedCard(original_pc.player_id, fake_card)

    engine.finalize_game()
    assert engine._deal_audit.audit_status == "OWNERSHIP_FAILED"
    assert state.phase in (GamePhase.GAME_OVER, GamePhase.DRAW)


def test_finalize_game_ownership_failed_duplicate_card() -> None:
    engine = _setup_tampered_game()
    state = engine.state
    # Replace second trick's first card with first trick's first card
    original = state.completed_tricks[0].played_cards[0]
    state.completed_tricks[1].played_cards[0] = PlayedCard(
        original.player_id, original.card,
    )

    engine.finalize_game()
    assert engine._deal_audit.audit_status == "OWNERSHIP_FAILED"
    assert state.phase in (GamePhase.GAME_OVER, GamePhase.DRAW)


def test_finalize_game_ownership_failed_missing_cards() -> None:
    engine = _setup_tampered_game()
    state = engine.state
    # Remove the last trick entirely
    state.completed_tricks.pop()

    engine.finalize_game()
    assert engine._deal_audit.audit_status == "OWNERSHIP_FAILED"
    assert state.phase in (GamePhase.GAME_OVER, GamePhase.DRAW)


def test_finalize_game_ownership_failed_fewer_than_48() -> None:
    engine = _setup_tampered_game()
    state = engine.state
    # Remove one played card from a trick
    state.completed_tricks[0].played_cards.pop()

    engine.finalize_game()
    assert engine._deal_audit.audit_status == "OWNERSHIP_FAILED"
    assert state.phase in (GamePhase.GAME_OVER, GamePhase.DRAW)


def test_finalize_game_ownership_failure_logs_only_safe_metadata(caplog) -> None:
    engine = _setup_tampered_game()
    state = engine.state
    trick = state.completed_tricks[0]
    original_pc = trick.played_cards[0]
    wrong_pid = state.seat_order[1] if original_pc.player_id == state.seat_order[0] else state.seat_order[0]
    trick.played_cards[0] = PlayedCard(wrong_pid, original_pc.card)

    caplog.set_level(logging.ERROR, logger="mendicot.engine")
    engine.finalize_game()

    error_record = next(
        r for r in caplog.records
        if r.getMessage() == "terminal ownership verification failed"
    )
    log_text = repr(error_record.__dict__)
    assert engine._deal_audit.server_secret.hex() not in log_text
    assert "dealt_hands" not in log_text
    assert "shuffled_deck" not in log_text
    assert error_record.error_category == "OWNERSHIP_VERIFICATION_FAILED"


def test_finalize_game_normal_trump_terminal_audit() -> None:
    engine = _setup_tampered_game(hidden=False)
    # Untampered — verify goes to VERIFIED
    engine.finalize_game()
    assert engine._deal_audit.audit_status == "VERIFIED"


def test_finalize_game_hidden_trump_terminal_audit() -> None:
    engine = _setup_tampered_game(hidden=True)
    engine.finalize_game()
    assert engine._deal_audit.audit_status == "VERIFIED"
