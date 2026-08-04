import dataclasses
import logging
import threading
from .enums import GamePhase, TrumpStatus, Suit
from .models import GameState, Player, Team, Card, Trick, PlayedCard, TrumpState
from .exceptions import (
    InvalidPhase,
    NotPlayersTurn,
    InvalidHiddenCardSelection,
    TrumpAlreadyRevealed,
    InvalidTrumpAction,
    DealAlreadyCompleted,
    DealInvariantFailed,
    ShuffleVerificationFailed,
    NotRoomHost,
    InvalidTrumpMode,
    InvalidCardIndex,
    NotTrumpHider,
)
from .deck import (
    DEALING_ALGORITHM_VERSION,
    DECK_DEFINITION_VERSION,
    deal_cards,
    validate_deal_invariants,
    validate_deck_definition,
)
from .secure_shuffle import (
    EntropySource,
    SHUFFLE_ALGORITHM_VERSION,
    SecureShuffleService,
    ShuffleAuditRecord,
    attach_dealt_hands,
    verify_played_card_ownership,
    verify_shuffle_audit,
)
from .validators import (
    validate_player_count,
    validate_team_configuration,
    validate_seating,
    validate_follow_suit,
    validate_card_ownership
)


logger = logging.getLogger(__name__)


class MendiCotEngine:
    """Core game engine for MendiCot card game.

    Manages game state and enforces all game rules through a phase-based
    state machine. Supports both normal and hidden trump modes.
    """

    MENDI_SUIT_ORDER = {
        Suit.SPADES: 0,
        Suit.HEARTS: 1,
        Suit.DIAMONDS: 2,
        Suit.CLUBS: 3,
    }

    def __init__(self, entropy_source: EntropySource | None = None):
        """Initialize with secure production entropy or explicit test injection."""
        self.state: GameState | None = None
        self._shuffle_service = SecureShuffleService(entropy_source)
        self._deal_lock = threading.RLock()
        self._deal_audit: ShuffleAuditRecord | None = None
        self._room_id: str | None = None
        # Private hidden card storage — physically removed from the hider's
        # hand at blind selection and returned during the reveal lifecycle.
        self._hidden_card: Card | None = None
        self._hidden_card_owner_id: str | None = None
        self._hidden_card_returned: bool = False
        self._reveal_actor_id: str | None = None

    def create_game(
        self,
        game_id: str,
        players: list[Player],
        host_id: str,
        hidden_trump_mode: bool = False,
        room_id: str | None = None,
    ) -> GameState:
        """Create a new game with validated players and seating."""

        player_count = len(players)
        validate_player_count(player_count)
        validate_team_configuration(players)
        validate_seating(players)
        validate_deck_definition()

        teams = {}
        for p in players:
            if p.team_id not in teams:
                teams[p.team_id] = Team(team_id=p.team_id, player_ids=[])
            teams[p.team_id].player_ids.append(p.player_id)

        sorted_players = sorted(players, key=lambda p: p.seat_index)
        seat_order = [p.player_id for p in sorted_players]

        self._room_id = room_id or game_id
        self._deal_audit = None
        self.state = GameState(
            game_id=game_id,
            player_count=player_count,
            players=players,
            teams=teams,
            captured_mendis={team_id: [] for team_id in teams},
            seat_order=seat_order,
            hands={},
            host_id=host_id,
            phase=GamePhase.CREATED,
            hidden_trump_mode=hidden_trump_mode
        )

        return self.state

    def select_trump_hider(
        self,
        actor_id_or_player_id: str,
        player_id: str | None = None
    ) -> GameState:
        """Commit the host-selected hider before dealing.

        The one-argument form remains an idempotent compatibility check during
        blind selection; it cannot change the committed post-deal hider.
        """
        with self._deal_lock:
            if player_id is not None:
                actor_id = actor_id_or_player_id
                if not self.state.hidden_trump_mode:
                    raise InvalidTrumpMode(
                        "SELECT_TRUMP_HIDER is only valid in hidden trump mode."
                    )
                if actor_id != self.state.host_id:
                    raise NotRoomHost(
                        "Only the host can select the trump hider."
                    )
                if (
                    self.state.dealt
                    or self.state.configuration_locked
                    or self.state.phase not in (
                        GamePhase.CREATED,
                        GamePhase.FIRST_PLAYER_SELECTION,
                    )
                ):
                    raise InvalidPhase(
                        "Trump hider selection is locked after dealing."
                    )
                if player_id not in self.state.seat_order:
                    raise ValueError("Player not in game.")
                if self.state.trump_state.trump_hider_id == player_id:
                    return self.state
                self.state.selected_trump_hider_id = player_id
                self.state.trump_state.trump_hider_id = player_id
                self.state.version += 1
                return self.state

            target_player_id = actor_id_or_player_id
            if self.state.phase != GamePhase.HIDDEN_TRUMP_SELECTION:
                raise InvalidPhase(
                    "Can only confirm the hider in HIDDEN_TRUMP_SELECTION phase."
                )
            if target_player_id not in self.state.seat_order:
                raise ValueError("Player not in game.")
            if self.state.trump_state.trump_hider_id != target_player_id:
                raise InvalidPhase("Trump hider was locked before dealing.")
            return self.state

    def select_first_player(
        self,
        actor_id: str,
        player_id: str
    ) -> GameState:
        """Host selects and locks the first player before dealing."""
        with self._deal_lock:
            if actor_id != self.state.host_id:
                raise NotRoomHost(
                    "Only the host can select the first player."
                )
            if (
                self.state.dealt
                or self.state.configuration_locked
                or self.state.phase not in (
                    GamePhase.CREATED,
                    GamePhase.FIRST_PLAYER_SELECTION,
                )
            ):
                raise InvalidPhase(
                    "First-player selection is locked after dealing."
                )
            if player_id not in self.state.seat_order:
                raise ValueError("Player not in game.")
            self.state.selected_first_player_id = player_id
            if self.state.phase == GamePhase.FIRST_PLAYER_SELECTION:
                self.state.phase = GamePhase.CREATED
            self.state.version += 1
            return self.state

    def begin_first_player_selection(self) -> GameState:
        """Enter the authoritative pre-deal first-player selection phase."""
        if self.state.phase != GamePhase.CREATED:
            raise InvalidPhase(
                "Can only begin first-player selection from CREATED phase."
            )
        self.state.phase = GamePhase.FIRST_PLAYER_SELECTION
        self.state.version += 1
        return self.state

    def deal_cards(self) -> GameState:
        """Atomically commit one secure round-robin deal for this game."""
        with self._deal_lock:
            if self.state.dealt:
                logger.warning(
                    "duplicate deal rejected",
                    extra={
                        "game_id": self.state.game_id,
                        "room_id": self._room_id,
                        "error_category": "DEAL_ALREADY_COMPLETED",
                    },
                )
                raise DealAlreadyCompleted()
            if self.state.phase != GamePhase.CREATED:
                raise InvalidPhase("Can only deal cards in CREATED phase.")
            if (
                self.state.hidden_trump_mode
                and self.state.trump_state.trump_hider_id is None
            ):
                raise InvalidPhase(
                    "Hidden-trump hider must be selected before dealing."
                )

            try:
                validate_deck_definition()
                validate_seating(self.state.players)
                ordered_players = sorted(
                    self.state.players,
                    key=lambda player: player.seat_index,
                )
                ordered_player_ids = [
                    player.player_id for player in ordered_players
                ]
                if ordered_player_ids != self.state.seat_order:
                    raise DealInvariantFailed()

                shuffled, audit_record = self._shuffle_service.prepare_shuffle(
                    game_id=self.state.game_id,
                    room_id=self._room_id or self.state.game_id,
                    players=ordered_players,
                    trump_mode=(
                        "hidden" if self.state.hidden_trump_mode else "normal"
                    ),
                    selected_first_player_id=(
                        self.state.selected_first_player_id
                    ),
                    selected_trump_hider_id=(
                        self.state.trump_state.trump_hider_id
                    ),
                )
                hands_list = deal_cards(shuffled, self.state.player_count)
                committed_hands = {
                    player_id: list(hands_list[index])
                    for index, player_id in enumerate(ordered_player_ids)
                }
                validate_deal_invariants(
                    committed_hands,
                    ordered_player_ids,
                )
                attach_dealt_hands(audit_record, committed_hands)
                verify_shuffle_audit(audit_record)
            except (
                DealInvariantFailed,
                ShuffleVerificationFailed,
            ) as exc:
                logger.error(
                    "secure deal validation failed",
                    extra={
                        "game_id": self.state.game_id,
                        "room_id": self._room_id,
                        "error_category": exc.__class__.__name__,
                    },
                )
                raise
            except Exception as exc:
                logger.error(
                    "secure deal failed",
                    extra={
                        "game_id": self.state.game_id,
                        "room_id": self._room_id,
                        "error_category": exc.__class__.__name__,
                    },
                )
                raise

            # Commit only after shuffle, ownership, and audit verification pass.
            self.state.hands = committed_hands
            self.state.dealt = True
            self.state.deal_generation += 1
            self.state.configuration_locked = True
            self.state.deck_definition_version = DECK_DEFINITION_VERSION
            self.state.shuffle_algorithm_version = SHUFFLE_ALGORITHM_VERSION
            self.state.dealing_algorithm_version = DEALING_ALGORITHM_VERSION
            self.state.shuffle_commitment = audit_record.commitment_hash
            self._deal_audit = audit_record

            if self.state.hidden_trump_mode:
                self.state.phase = GamePhase.HIDDEN_TRUMP_SELECTION
                self.state.current_turn = None
            else:
                self.state.phase = GamePhase.PLAYING
                self.state.current_turn = (
                    self.state.selected_first_player_id
                    or self.state.seat_order[0]
                )

            self.state.version += 1
            logger.info(
                "secure deal committed",
                extra={
                    "game_id": self.state.game_id,
                    "room_id": self._room_id,
                    "shuffle_algorithm_version": SHUFFLE_ALGORITHM_VERSION,
                    "deck_definition_version": DECK_DEFINITION_VERSION,
                    "commitment_hash": audit_record.commitment_hash,
                    "deck_hash": audit_record.shuffled_deck_hash,
                    "player_count": self.state.player_count,
                    "deal_status": "SUCCESS",
                },
            )
            return self.state

    def verify_deal_audit(self, **overrides) -> bool:
        """Verify the private committed deal without exposing its secret."""
        if self._deal_audit is None:
            raise ShuffleVerificationFailed("No committed shuffle audit exists.")
        return verify_shuffle_audit(self._deal_audit, **overrides)

    def get_shuffle_audit(self, reveal: bool = False) -> dict[str, object]:
        """Return public commitment metadata, revealing only after game end."""
        if self._deal_audit is None:
            return {}
        if reveal and self.state.phase not in (GamePhase.GAME_OVER, GamePhase.DRAW):
            raise InvalidPhase("Shuffle secret is unavailable during gameplay.")
        return self._deal_audit.public_metadata(reveal=reveal)

    def select_hidden_card(self, player_id: str, card_position: object) -> GameState:
        """Select a card by position for hidden trump (blind selection).

        The selected card is physically removed from the hider's playable
        hand and stored in private engine state.  It cannot be played
        before the two-stage reveal lifecycle returns it.
        """

        if self.state.phase != GamePhase.HIDDEN_TRUMP_SELECTION:
            raise InvalidPhase()

        if player_id != self.state.trump_state.trump_hider_id:
            raise NotPlayersTurn("Not the selected trump hider.")

        # Strict type validation: reject None, str, float, bool before
        # any numeric comparison.  Python's bool is a subclass of int,
        # so isinstance(True, int) is True — check bool first.
        if (
            card_position is None
            or isinstance(card_position, bool)
            or not isinstance(card_position, int)
        ):
            raise InvalidCardIndex()

        hand = self.state.hands[player_id]

        if card_position < 0 or card_position >= len(hand):
            raise InvalidCardIndex()

        selected_card = hand[card_position]

        # Physically remove the card from the hider's playable hand.
        hand.pop(card_position)
        self._hidden_card = selected_card
        self._hidden_card_owner_id = player_id
        self._hidden_card_returned = False

        self.state.trump_state.hidden_card_index = card_position
        self.state.trump_state.suit = selected_card.suit
        self.state.trump_state.hidden_rank = selected_card.rank

        self.state.phase = GamePhase.HIDDEN_TRUMP_REVEAL
        self.state.version += 1

        return self.state

    def complete_hidden_trump_setup(self, player_id: str | None = None) -> GameState:
        """Complete hidden trump setup after temporary reveal."""

        if self.state.phase != GamePhase.HIDDEN_TRUMP_REVEAL:
            raise InvalidPhase()

        hider_id = self.state.trump_state.trump_hider_id

        if player_id is not None and player_id != hider_id:
            raise NotTrumpHider()

        self.state.trump_state.status = TrumpStatus.HIDDEN

        self.state.current_turn = (
            self.state.selected_first_player_id
            or self._get_next_player(hider_id)
        )

        self.state.phase = GamePhase.PLAYING
        self.state.version += 1

        return self.state

    def play_card(self, player_id: str, card: Card) -> GameState:
        """Play a card in the current trick."""

        if self.state.phase != GamePhase.PLAYING:
            raise InvalidPhase()

        if player_id != self.state.current_turn:
            raise NotPlayersTurn()

        hand = self.state.hands[player_id]
        validate_card_ownership(hand, card)

        trick = self.state.current_trick

        if not trick.played_cards:
            trick.lead_player_id = player_id
            trick.lead_suit = card.suit

        else:
            is_on_cut = not any(
                c.suit == trick.lead_suit
                for c in hand
            )

            validate_follow_suit(
                hand,
                trick.lead_suit,
                card,
                self.state.trump_state
            )

            # Normal mode: first cut creates public trump.
            if (
                is_on_cut
                and self.state.trump_state.status == TrumpStatus.NONE
                and not self.state.hidden_trump_mode
            ):
                self.state.trump_state.suit = card.suit
                self.state.trump_state.status = TrumpStatus.PUBLIC

        hand.remove(card)
        trick.played_cards.append(
            PlayedCard(player_id, card)
        )

        if len(trick.played_cards) == self.state.player_count:
            trick.winner_player_id = self._determine_trick_winner(
                trick, self.state.trump_state
            )
            trick.completed = True
            self.state.phase = GamePhase.TRICK_RESOLUTION
            self.state.current_turn = None
        else:
            self.state.current_turn = self._get_next_player(player_id)

        self.state.version += 1
        return self.state

    def reveal_trump(self, player_id: str) -> GameState:
        """Reveal the hidden trump suit and enter the two-stage reveal lifecycle.

        PLAYING → TRUMP_REVEAL_DISPLAY → (timer) → HIDDEN_CARD_RETURN → (timer) → PLAYING

        Can only be called when the player is on cut (has no lead-suit cards).
        After the lifecycle completes, the same player continues the same turn
        and must still play a card via play_card().

        Args:
            player_id: The player choosing to reveal trump.

        Raises:
            InvalidPhase: If not in PLAYING phase.
            NotPlayersTurn: If it's not this player's turn.
            TrumpAlreadyRevealed: If trump is already PUBLIC.
            InvalidTrumpAction: If player is not on cut or no trick in progress.
        """
        if self.state.phase != GamePhase.PLAYING:
            raise InvalidPhase()
        if player_id != self.state.current_turn:
            raise NotPlayersTurn()

        trick = self.state.current_trick
        if not trick.played_cards or trick.lead_suit is None:
            raise InvalidTrumpAction("Cannot reveal trump on first card of trick.")

        if self.state.trump_state.status != TrumpStatus.HIDDEN:
            raise TrumpAlreadyRevealed()

        if not self._is_on_cut(player_id):
            raise InvalidTrumpAction("Must be on cut to reveal trump.")

        # Make trump suit public and enter the reveal display phase.
        # current_turn and current_trick are intentionally preserved.
        self.state.trump_state.status = TrumpStatus.PUBLIC
        self._reveal_actor_id = player_id
        self.state.phase = GamePhase.TRUMP_REVEAL_DISPLAY
        self.state.reveal_generation += 1
        self.state.version += 1
        return self.state

    def complete_trump_reveal_display(self) -> GameState:
        """Timer-driven transition: return hidden card to hider, enter HIDDEN_CARD_RETURN.

        The hidden card is physically moved from private engine storage back
        into the original Trump Hider's authoritative hand.  This happens
        exactly once (guarded by ``_hidden_card_returned``).

        Raises:
            InvalidPhase: If not in TRUMP_REVEAL_DISPLAY phase.
        """
        if self.state.phase != GamePhase.TRUMP_REVEAL_DISPLAY:
            raise InvalidPhase()

        if not self._hidden_card_returned and self._hidden_card is not None:
            hider_id = self._hidden_card_owner_id
            card = self._hidden_card

            # Validate no duplicate ownership.
            assert card not in self.state.hands[hider_id], (
                "Hidden card already present in hider's hand before return."
            )

            self.state.hands[hider_id].append(card)
            self._hidden_card = None
            self._hidden_card_returned = True

        self.state.phase = GamePhase.HIDDEN_CARD_RETURN
        self.state.version += 1
        return self.state

    def complete_hidden_card_return(self) -> GameState:
        """Timer-driven transition: return to PLAYING after hidden card return display.

        current_turn and current_trick are intentionally unchanged — the same
        player continues the same turn.

        Raises:
            InvalidPhase: If not in HIDDEN_CARD_RETURN phase.
        """
        if self.state.phase != GamePhase.HIDDEN_CARD_RETURN:
            raise InvalidPhase()

        self.state.phase = GamePhase.PLAYING
        self.state.version += 1
        return self.state

    def resolve_trick(self) -> GameState:
        """Resolve the current completed trick.

        Determines the winner, awards captured cards and tens,
        and transitions to the next trick or end-of-game.

        Raises:
            InvalidPhase: If not in TRICK_RESOLUTION phase.
        """
        if self.state.phase != GamePhase.TRICK_RESOLUTION:
            raise InvalidPhase()

        trick = self.state.current_trick
        winner_id = self._determine_trick_winner(trick, self.state.trump_state)
        trick.winner_player_id = winner_id
        trick.completed = True

        winner_team_id = next(p.team_id for p in self.state.players if p.player_id == winner_id)
        team = self.state.teams[winner_team_id]

        captured = [pc.card for pc in trick.played_cards]
        team.captured_cards.extend(captured)
        team.tricks_won += 1

        for team_id in self.state.teams:
            self.state.captured_mendis.setdefault(team_id, [])
        owned_mendi_suits = {
            suit
            for suits in self.state.captured_mendis.values()
            for suit in suits
        }
        for card in captured:
            if card.is_ten and card.suit not in owned_mendi_suits:
                self.state.captured_mendis[winner_team_id].append(card.suit)
                owned_mendi_suits.add(card.suit)
        for team_id, suits in self.state.captured_mendis.items():
            suits.sort(key=self.MENDI_SUIT_ORDER.__getitem__)
            self.state.teams[team_id].tens_captured = len(suits)

        self.state.completed_tricks.append(trick)
        self.state.current_trick = Trick()

        all_empty = all(len(h) == 0 for h in self.state.hands.values())
        if all_empty:
            winner = self.determine_winner()
            self.state.final_result = winner or "DRAW"
            self.state.phase = GamePhase.FINAL_SCORE_DISPLAY
            self.state.current_turn = None
        else:
            self.state.current_turn = winner_id
            self.state.phase = GamePhase.PLAYING

        self.state.version += 1
        return self.state

    def finalize_game(self) -> GameState:
        """Commit a resolved final scoreboard to its terminal result phase.

        Terminal verification is best-effort: ownership-audit failures are
        recorded but never prevent the game result from completing.  Players
        always reach GAME_OVER or DRAW.
        """
        if self.state.phase != GamePhase.FINAL_SCORE_DISPLAY:
            raise InvalidPhase("Final score display is not active.")

        if self._deal_audit is not None:
            try:
                # 1. Verify commitment, shuffle reproduction, and dealt hands.
                verify_shuffle_audit(self._deal_audit)

                # 2. Collect complete authoritative played-card history.
                played_cards: list[tuple[str, Card]] = [
                    (pc.player_id, pc.card)
                    for trick in self.state.completed_tricks
                    for pc in trick.played_cards
                ]

                # 3. Verify every played card was originally owned by the
                #    player recorded as playing it, that all 48 cards were
                #    played exactly once, and that no fabricated, duplicated,
                #    or missing cards exist.
                verify_played_card_ownership(
                    self._deal_audit,
                    played_cards,
                    require_complete=True,
                )

                self._deal_audit.audit_status = "VERIFIED"
            except ShuffleVerificationFailed:
                # Record the failure safely without exposing hands/secrets.
                self._deal_audit.audit_status = "OWNERSHIP_FAILED"
                logger.error(
                    "terminal ownership verification failed",
                    extra={
                        "game_id": self.state.game_id,
                        "room_id": self._room_id,
                        "error_category": "OWNERSHIP_VERIFICATION_FAILED",
                        "played_card_count": len(
                            [
                                pc
                                for trick in self.state.completed_tricks
                                for pc in trick.played_cards
                            ]
                        ),
                    },
                )
                # Do NOT re-raise — players must reach the result screen.

        if self.state.final_result is None:
            self.state.final_result = self.determine_winner() or "DRAW"
        self.state.phase = (
            GamePhase.DRAW
            if self.state.final_result == "DRAW"
            else GamePhase.GAME_OVER
        )
        self.state.current_turn = None
        self.state.version += 1
        return self.state

    def calculate_score(self) -> dict[str, dict]:
        """Return current scoring information for all teams.

        Returns:
            Dict mapping team_id to {'tens_captured': int, 'tricks_won': int}.
        """
        scores = {}
        for team_id, team in self.state.teams.items():
            scores[team_id] = {
                "tens_captured": team.tens_captured,
                "tricks_won": team.tricks_won
            }
        return scores

    def determine_winner(self) -> str | None:
        """Determine the winning team based on scoring rules.

        Compares tens captured first, then tricks won as tiebreaker.

        Returns:
            Winning team_id, or None if the result is a DRAW.
        """
        teams = list(self.state.teams.values())
        t1, t2 = teams[0], teams[1]

        if t1.tens_captured != t2.tens_captured:
            return t1.team_id if t1.tens_captured > t2.tens_captured else t2.team_id

        if t1.tricks_won != t2.tricks_won:
            return t1.team_id if t1.tricks_won > t2.tricks_won else t2.team_id

        return None

    def _determine_trick_winner(self, trick: Trick, trump_state: TrumpState) -> str:
        lead_suit = trick.lead_suit
        effective_trump_suit = trump_state.suit if trump_state.status == TrumpStatus.PUBLIC else None

        best_card = trick.played_cards[0].card
        winner_id = trick.played_cards[0].player_id

        for pc in trick.played_cards[1:]:
            card = pc.card

            if effective_trump_suit is not None:
                if best_card.suit == effective_trump_suit:
                    if card.suit == effective_trump_suit and card.rank > best_card.rank:
                        best_card = card
                        winner_id = pc.player_id
                else:
                    if card.suit == effective_trump_suit:
                        best_card = card
                        winner_id = pc.player_id
                    elif card.suit == lead_suit and card.rank > best_card.rank:
                        best_card = card
                        winner_id = pc.player_id
            else:
                if card.suit == lead_suit and best_card.suit == lead_suit:
                    if card.rank > best_card.rank:
                        best_card = card
                        winner_id = pc.player_id
                elif card.suit == lead_suit and best_card.suit != lead_suit:
                    best_card = card
                    winner_id = pc.player_id

        return winner_id

    def get_current_trick_leader(self) -> PlayedCard | None:
        """Return the provisional winner of the public cards in the active trick.

        This intentionally delegates to the same comparison routine used by
        resolve_trick so an incomplete trick cannot drift from final trick
        resolution, including while trump is hidden.
        """
        trick = self.state.current_trick
        if not trick.played_cards:
            return None

        winner_id = self._determine_trick_winner(trick, self.state.trump_state)
        return next(
            played_card
            for played_card in trick.played_cards
            if played_card.player_id == winner_id
        )

    def _get_next_player(self, current_player_id: str) -> str:
        order = self.state.seat_order
        idx = order.index(current_player_id)
        return order[(idx + 1) % len(order)]

    def _is_on_cut(self, player_id: str) -> bool:
        hand = self.state.hands[player_id]
        lead_suit = self.state.current_trick.lead_suit
        return not any(c.suit == lead_suit for c in hand)

    def get_player_view(self, player_id: str) -> dict:
        """Return a sanitized view of the game state safe for the given player."""

        state_dict = dataclasses.asdict(self.state)
        state_dict["captured_mendis"] = {
            team_id: [
                suit.value
                for suit in sorted(
                    self.state.captured_mendis.get(team_id, []),
                    key=self.MENDI_SUIT_ORDER.__getitem__,
                )
            ]
            for team_id in self.state.teams
        }

        leader = self.get_current_trick_leader()
        if leader is None:
            state_dict["current_trick_leader"] = None
        else:
            leader_player = next(
                player
                for player in self.state.players
                if player.player_id == leader.player_id
            )
            state_dict["current_trick_leader"] = {
                "player_id": leader.player_id,
                "display_name": leader_player.display_name or leader_player.player_id,
                "card": dataclasses.asdict(leader.card),
            }

        # Only include this player's hand. Hidden-trump selection is blind:
        # the hider receives authoritative positions but no card identities.
        own_hand = state_dict["hands"].get(player_id, [])
        is_hider = (player_id == self.state.trump_state.trump_hider_id)
        if (
            self.state.phase == GamePhase.HIDDEN_TRUMP_SELECTION
            and is_hider
        ):
            state_dict["hands"] = {player_id: []}
            state_dict["hidden_hand_positions"] = list(
                range(len(self.state.hands.get(player_id, [])))
            )
        else:
            state_dict["hands"] = {player_id: own_hand}
            state_dict["hidden_hand_positions"] = None

        reveal_audit = (
            self.state.phase in (GamePhase.GAME_OVER, GamePhase.DRAW)
            and self._deal_audit is not None
            and self._deal_audit.audit_status == "VERIFIED"
        )
        state_dict["shuffle_audit"] = (
            self._deal_audit.public_metadata(reveal=reveal_audit)
            if self._deal_audit is not None
            else {}
        )

        trump_status = self.state.trump_state.status

        if self.state.phase == GamePhase.HIDDEN_TRUMP_REVEAL and is_hider:
            # The exact card is visible only to its hider during temporary reveal.
            pass
        else:
            # Rank and original hand position are never public. Once trump is
            # public, only its suit is exposed.
            state_dict["trump_state"]["hidden_rank"] = None
            state_dict["trump_state"]["hidden_card_index"] = None
            if trump_status != TrumpStatus.PUBLIC:
                state_dict["trump_state"]["suit"] = None

        # --- Two-stage reveal lifecycle metadata ---
        if self.state.phase == GamePhase.TRUMP_REVEAL_DISPLAY:
            state_dict["trump_reveal_display"] = {
                "trump_hider_id": self.state.trump_state.trump_hider_id,
                "reveal_actor_id": self._reveal_actor_id,
            }
        else:
            state_dict["trump_reveal_display"] = None

        if self.state.phase == GamePhase.HIDDEN_CARD_RETURN:
            state_dict["hidden_card_return"] = {
                "hider_id": self._hidden_card_owner_id,
                "returned": self._hidden_card_returned,
            }
        else:
            state_dict["hidden_card_return"] = None

        return state_dict
