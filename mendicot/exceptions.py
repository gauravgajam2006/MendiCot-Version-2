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
