from .enums import Suit, TeamId, TrumpStatus
from .models import Player, Card, TrumpState
from .exceptions import (
    InvalidPlayerCount,
    InvalidTeamConfiguration,
    InvalidSeatArrangement,
    InvalidSeatConfiguration,
    MustFollowSuit,
    MustPlayTrump,
    CardNotOwned
)

def validate_player_count(count: int) -> None:
    if count not in (4, 6, 8):
        raise InvalidPlayerCount()

def validate_team_configuration(players: list[Player]) -> None:
    team_counts = {}
    for player in players:
        team_counts[player.team_id] = team_counts.get(player.team_id, 0) + 1
    
    if set(team_counts) != {TeamId.TEAM_A.value, TeamId.TEAM_B.value}:
        raise InvalidTeamConfiguration()
    
    counts = list(team_counts.values())
    if counts[0] != counts[1]:
        raise InvalidTeamConfiguration()

def validate_seating(players: list[Player]) -> None:
    num_players = len(players)
    player_ids = [player.player_id for player in players]
    seat_indexes = [player.seat_index for player in players]
    if len(set(player_ids)) != num_players:
        raise InvalidSeatConfiguration("Player IDs must be unique.")
    if any(
        not isinstance(seat_index, int) or isinstance(seat_index, bool)
        for seat_index in seat_indexes
    ):
        raise InvalidSeatConfiguration("Seat indexes must be integers.")
    if len(set(seat_indexes)) != num_players:
        raise InvalidSeatConfiguration("Seat indexes must be unique.")
    if set(seat_indexes) != set(range(num_players)):
        raise InvalidSeatConfiguration(
            "Seat indexes must form the complete zero-based range."
        )

    sorted_players = sorted(players, key=lambda p: p.seat_index)
    for i in range(num_players):
        current_team = sorted_players[i].team_id
        next_team = sorted_players[(i + 1) % num_players].team_id
        if current_team == next_team:
            raise InvalidSeatArrangement()

def validate_follow_suit(hand: list[Card], lead_suit: Suit, card: Card, trump_state: TrumpState) -> None:
    has_lead_suit = any(c.suit == lead_suit for c in hand)
    
    if has_lead_suit and card.suit != lead_suit:
        raise MustFollowSuit()
    
    if not has_lead_suit:
        # On cut
        if trump_state.status == TrumpStatus.PUBLIC and trump_state.suit is not None:
            has_trump = any(c.suit == trump_state.suit for c in hand)
            if has_trump and card.suit != trump_state.suit:
                raise MustPlayTrump()

def validate_card_ownership(hand: list[Card], card: Card) -> None:
    if card not in hand:
        raise CardNotOwned()
