"""Utilities for maintaining canonical room identifiers."""


def normalize_room_id(room_id: str) -> str:
    """Return the canonical form used for room storage and comparisons."""
    return room_id.strip().lower()