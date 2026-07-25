from dataclasses import dataclass, field
from .enums import Suit, Rank, GamePhase, TrumpStatus

@dataclass(frozen=True)
class Card:
    suit: Suit
    rank: Rank

    @property
    def is_ten(self) -> bool:
        return self.rank == Rank.TEN

    def __str__(self) -> str:
        suit_symbols = {
            Suit.SPADES: "♠",
            Suit.HEARTS: "♥",
            Suit.DIAMONDS: "♦",
            Suit.CLUBS: "♣"
        }
        rank_names = {
            Rank.JACK: "J",
            Rank.QUEEN: "Q",
            Rank.KING: "K",
            Rank.ACE: "A"
        }
        rank_str = rank_names.get(self.rank, str(self.rank.value))
        return f"{rank_str}{suit_symbols[self.suit]}"

@dataclass(frozen=True)
class Player:
    player_id: str
    team_id: str
    seat_index: int

@dataclass
class Team:
    team_id: str
    player_ids: list[str]
    captured_cards: list[Card] = field(default_factory=list)
    tricks_won: int = 0
    tens_captured: int = 0

@dataclass(frozen=True)
class PlayedCard:
    player_id: str
    card: Card

@dataclass
class Trick:
    lead_player_id: str | None = None
    lead_suit: Suit | None = None
    played_cards: list[PlayedCard] = field(default_factory=list)
    winner_player_id: str | None = None
    completed: bool = False

@dataclass
class TrumpState:
    status: TrumpStatus = TrumpStatus.NONE
    suit: Suit | None = None
    hidden_rank: Rank | None = None
    hidden_card_index: int | None = None
    trump_hider_id: str | None = None   

@dataclass
class GameState:
    game_id: str
    player_count: int
    players: list[Player]
    teams: dict[str, Team]
    seat_order: list[str]
    hands: dict[str, list[Card]]
    host_id: str | None = None
    selected_trump_hider_id: str | None = None
    selected_first_player_id: str | None = None

    phase: GamePhase = GamePhase.CREATED
    current_turn: str | None = None
    current_trick: Trick = field(default_factory=Trick)
    completed_tricks: list[Trick] = field(default_factory=list)
    trump_state: TrumpState = field(default_factory=TrumpState)
    version: int = 0
    hidden_trump_mode: bool = False
