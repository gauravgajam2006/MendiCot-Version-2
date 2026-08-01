class MendiCotError(Exception):
    """Base exception for MendiCot engine."""
    pass

class InvalidPlayerCount(MendiCotError):
    def __init__(self, message="Player count must be 4, 6, or 8."):
        super().__init__(message)

class InvalidTeamConfiguration(MendiCotError):
    def __init__(self, message="Teams must be exactly 2 with equal sizes."):
        super().__init__(message)

class InvalidPhase(MendiCotError):
    def __init__(self, message="Invalid phase for this action."):
        super().__init__(message)

class NotPlayersTurn(MendiCotError):
    def __init__(self, message="It is not this player's turn."):
        super().__init__(message)

class CardNotOwned(MendiCotError):
    def __init__(self, message="Player does not own this card."):
        super().__init__(message)

class MustFollowSuit(MendiCotError):
    def __init__(self, message="Player must follow the lead suit."):
        super().__init__(message)

class InvalidHiddenCardSelection(MendiCotError):
    def __init__(self, message="Invalid hidden card selection."):
        super().__init__(message)

class TrumpAlreadyRevealed(MendiCotError):
    def __init__(self, message="Trump is already revealed."):
        super().__init__(message)

class InvalidTrumpAction(MendiCotError):
    def __init__(self, message="Invalid trump action."):
        super().__init__(message)

class GameAlreadyFinished(MendiCotError):
    def __init__(self, message="Game is already finished."):
        super().__init__(message)

class InvalidSeatArrangement(MendiCotError):
    def __init__(self, message="Invalid seating arrangement."):
        super().__init__(message)

class MustPlayTrump(MendiCotError):
    def __init__(self, message="Player must play a trump card."):
        super().__init__(message)

class RoomError(MendiCotError):
    """Base exception for room management errors."""

class RoomAlreadyExists(RoomError):
    """Raised when creating a room with a duplicate ID."""

class RoomNotFound(RoomError):
    """Raised when a requested room does not exist."""

class DuplicatePlayer(RoomError):
    """Raised when a player with the same ID is already in the room."""

class RoomFull(RoomError):
    """Raised when the room has reached maximum capacity (8)."""

class GameAlreadyStarted(RoomError):
    """Raised when an action is invalid because the game has started."""

class InvalidRoomSize(RoomError):
    """Raised when trying to start with an invalid player count."""

class NotRoomHost(RoomError):
    """Raised when a non-host attempts a host-only action."""

class PlayerNotInRoom(RoomError):
    """Raised when referencing a player not in the room."""

class InvalidTeam(RoomError):
    """Raised when a lobby team ID is not supported."""

class RoomNotFull(RoomError):
    """Raised when starting before the configured room capacity is reached."""

class PlayerOffline(RoomError):
    """Raised when starting while one or more lobby players are offline."""

class TeamsUnbalanced(RoomError):
    """Raised when lobby teams are invalid or are not equally sized."""
