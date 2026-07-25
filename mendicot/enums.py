from enum import Enum, IntEnum

class Suit(str, Enum):
    SPADES = "SPADES"
    HEARTS = "HEARTS"
    DIAMONDS = "DIAMONDS"
    CLUBS = "CLUBS"

class Rank(IntEnum):
    THREE = 3
    FOUR = 4
    FIVE = 5
    SIX = 6
    SEVEN = 7
    EIGHT = 8
    NINE = 9
    TEN = 10
    JACK = 11
    QUEEN = 12
    KING = 13
    ACE = 14

class GamePhase(str, Enum):
    CREATED = "CREATED"
    DEALING = "DEALING"
    HIDDEN_TRUMP_SELECTION = "HIDDEN_TRUMP_SELECTION"
    HIDDEN_TRUMP_REVEAL = "HIDDEN_TRUMP_REVEAL"
    PLAYING = "PLAYING"
    TRICK_RESOLUTION = "TRICK_RESOLUTION"
    GAME_OVER = "GAME_OVER"
    DRAW = "DRAW"

class TrumpStatus(str, Enum):
    NONE = "NONE"
    HIDDEN = "HIDDEN"
    PUBLIC = "PUBLIC"

class ActionType(str, Enum):
    PLAY_CARD = "PLAY_CARD"
    REVEAL_TRUMP = "REVEAL_TRUMP"
    DO_NOT_REVEAL = "DO_NOT_REVEAL"
