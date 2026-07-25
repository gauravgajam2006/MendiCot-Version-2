import random
import dataclasses
from .enums import GamePhase, TrumpStatus, Suit
from .models import GameState, Player, Team, Card, Trick, PlayedCard, TrumpState
from .exceptions import (
    InvalidPhase,
    NotPlayersTurn,
    InvalidHiddenCardSelection,
    TrumpAlreadyRevealed,
    InvalidTrumpAction
)
from .deck import create_deck, shuffle_deck, deal_cards
from .validators import (
    validate_player_count,
    validate_team_configuration,
    validate_seating,
    validate_follow_suit,
    validate_card_ownership
)


class MendiCotEngine:
    """Core game engine for MendiCot card game.

    Manages game state and enforces all game rules through a phase-based
    state machine. Supports both normal and hidden trump modes.
    """

    def __init__(self, rng: random.Random | None = None):
        """Initialize the engine.

        Args:
            rng: Optional random.Random instance for deterministic testing.
                 If None, uses SystemRandom for secure shuffling.
        """
        self.rng = rng
        self.state: GameState | None = None

    def create_game(
        self,
        game_id: str,
        players: list[Player],
        host_id: str,
        hidden_trump_mode: bool = False
    ) -> GameState:
        """Create a new game with validated players and seating."""

        player_count = len(players)
        validate_player_count(player_count)
        validate_team_configuration(players)
        validate_seating(players)

        teams = {}
        for p in players:
            if p.team_id not in teams:
                teams[p.team_id] = Team(team_id=p.team_id, player_ids=[])
            teams[p.team_id].player_ids.append(p.player_id)

        sorted_players = sorted(players, key=lambda p: p.seat_index)
        seat_order = [p.player_id for p in sorted_players]

        self.state = GameState(
            game_id=game_id,
            player_count=player_count,
            players=players,
            teams=teams,
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
        """Select the player who will hide the trump.

        Two calling conventions:
        - select_trump_hider(actor_id, player_id): Host authorization required.
        - select_trump_hider(player_id): During HIDDEN_TRUMP_SELECTION phase.
        """
        if player_id is not None:
            # Host authorization version
            actor_id = actor_id_or_player_id
            if actor_id != self.state.host_id:
                raise PermissionError(
                    "Only the host can select the trump hider."
                )
            if player_id not in self.state.seat_order:
                raise ValueError("Player not in game.")
            self.state.selected_trump_hider_id = player_id
            self.state.trump_state.trump_hider_id = player_id
        else:
            # In-phase designation
            target_player_id = actor_id_or_player_id
            if self.state.phase != GamePhase.HIDDEN_TRUMP_SELECTION:
                raise InvalidPhase(
                    "Can only select trump hider in HIDDEN_TRUMP_SELECTION phase."
                )
            if target_player_id not in self.state.seat_order:
                raise ValueError("Player not in game.")
            self.state.trump_state.trump_hider_id = target_player_id

        self.state.version += 1
        return self.state

    def select_first_player(
        self,
        actor_id: str,
        player_id: str
    ) -> GameState:
        """Host selects the first player to lead."""
        if actor_id != self.state.host_id:
            raise PermissionError(
                "Only the host can select the first player."
            )
        if player_id not in self.state.seat_order:
            raise ValueError("Player not in game.")
        self.state.selected_first_player_id = player_id
        self.state.version += 1
        return self.state

    def deal_cards(self) -> GameState:
        """Shuffle and deal cards to all players.

        Transitions from CREATED to PLAYING (normal) or HIDDEN_TRUMP_SELECTION (hidden).

        Raises:
            InvalidPhase: If not in CREATED phase.
        """
        if self.state.phase != GamePhase.CREATED:
            raise InvalidPhase("Can only deal cards in CREATED phase.")

        deck = create_deck()
        shuffled = shuffle_deck(deck, self.rng)
        hands_list = deal_cards(shuffled, self.state.player_count)

        for i, player_id in enumerate(self.state.seat_order):
            self.state.hands[player_id] = hands_list[i]

        if self.state.hidden_trump_mode:
            self.state.phase = GamePhase.HIDDEN_TRUMP_SELECTION
        else:
            self.state.phase = GamePhase.PLAYING
            self.state.current_turn = (
                self.state.selected_first_player_id
                or self.state.seat_order[0]
            )

        self.state.version += 1
        return self.state

    def select_hidden_card(self, player_id: str, card_position: int) -> GameState:
        """Select a card by position for hidden trump (blind selection)."""

        if self.state.phase != GamePhase.HIDDEN_TRUMP_SELECTION:
            raise InvalidPhase()

        if player_id != self.state.trump_state.trump_hider_id:
            raise NotPlayersTurn("Not the selected trump hider.")

        hand = self.state.hands[player_id]

        if not (0 <= card_position < len(hand)):
            raise InvalidHiddenCardSelection()

        selected_card = hand[card_position]

        self.state.trump_state.hidden_card_index = card_position
        self.state.trump_state.suit = selected_card.suit
        self.state.trump_state.hidden_rank = selected_card.rank

        self.state.phase = GamePhase.HIDDEN_TRUMP_REVEAL
        self.state.version += 1

        return self.state

    def complete_hidden_trump_setup(self) -> GameState:
        """Complete hidden trump setup after temporary reveal."""

        if self.state.phase != GamePhase.HIDDEN_TRUMP_REVEAL:
            raise InvalidPhase()

        hider_id = self.state.trump_state.trump_hider_id

        self.state.trump_state.status = TrumpStatus.HIDDEN

        self.state.current_turn = self._get_next_player(hider_id)

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

            # Automatically reveal hidden trump when a player is
            # on cut and has at least one trump card.
            if (
                is_on_cut
                and self.state.trump_state.status == TrumpStatus.HIDDEN
                and self.state.trump_state.suit is not None
            ):
                has_trump = any(
                    c.suit == self.state.trump_state.suit
                    for c in hand
                )

                if has_trump:
                    self.state.trump_state.status = TrumpStatus.PUBLIC

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
            self.state.phase = GamePhase.TRICK_RESOLUTION
            self.state.current_turn = None
        else:
            self.state.current_turn = self._get_next_player(player_id)

        self.state.version += 1
        return self.state

    def reveal_trump(self, player_id: str) -> GameState:
        """Reveal the hidden trump suit (separate action from playing a card).

        Can only be called when the player is on cut (has no lead-suit cards).
        After this call, the player must still play a card via play_card().

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

        self.state.trump_state.status = TrumpStatus.PUBLIC
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
        team.tens_captured += sum(1 for c in captured if c.is_ten)

        self.state.completed_tricks.append(trick)
        self.state.current_trick = Trick()

        all_empty = all(len(h) == 0 for h in self.state.hands.values())
        if all_empty:
            winner = self.determine_winner()
            if winner is None:
                self.state.phase = GamePhase.DRAW
            else:
                self.state.phase = GamePhase.GAME_OVER
            self.state.current_turn = None
        else:
            self.state.current_turn = winner_id
            self.state.phase = GamePhase.PLAYING

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
        trump_suit = trump_state.suit if trump_state.status in (TrumpStatus.PUBLIC, TrumpStatus.HIDDEN) else None

        best_card = trick.played_cards[0].card
        winner_id = trick.played_cards[0].player_id

        for pc in trick.played_cards[1:]:
            card = pc.card

            if trump_suit is not None:
                if best_card.suit == trump_suit:
                    if card.suit == trump_suit and card.rank > best_card.rank:
                        best_card = card
                        winner_id = pc.player_id
                else:
                    if card.suit == trump_suit:
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

        # Only include this player's hand.
        state_dict["hands"] = {
            player_id: state_dict["hands"].get(player_id, [])
        }

        trump_status = self.state.trump_state.status

        if self.state.phase == GamePhase.HIDDEN_TRUMP_REVEAL:
            # During temporary reveal, hidden card information is visible.
            pass

        elif trump_status != TrumpStatus.PUBLIC:
            # Hide the hidden card from the player's view.
            state_dict["trump_state"]["hidden_rank"] = None
            state_dict["trump_state"]["hidden_card_index"] = None

        return state_dict