"""Runtime-only associations between authenticated sessions and push targets."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mendicot.room_ids import normalize_room_id

MAX_REGISTRATION_ID_LENGTH = 4096


def session_identity(session_token: str) -> str:
    return hashlib.sha256(session_token.encode("utf-8")).hexdigest()


@dataclass
class PushRegistration:
    room_id: str
    player_id: str
    session_identity: str
    enabled: bool
    registered_at: datetime
    last_seen: datetime
    _registration_id: str = field(repr=False)

    @property
    def registration_id(self) -> str:
        return self._registration_id


class PushRegistrationStore:
    """In-memory registrations keyed by authoritative room/player/session."""

    def __init__(self) -> None:
        self._registrations: dict[tuple[str, str, str], PushRegistration] = {}

    @staticmethod
    def _key(room_id: str, player_id: str, identity: str) -> tuple[str, str, str]:
        return normalize_room_id(room_id), player_id, identity

    def register(self, room_id: str, player_id: str, session_token: str,
                 registration_id: object, enabled: object) -> PushRegistration:
        if not isinstance(registration_id, str) or not registration_id.strip():
            raise ValueError("registration_id must be a non-empty string.")
        registration_id = registration_id.strip()
        if len(registration_id) > MAX_REGISTRATION_ID_LENGTH:
            raise ValueError("registration_id is too long.")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean.")
        identity = session_identity(session_token)
        key = self._key(room_id, player_id, identity)
        now = datetime.now(timezone.utc)
        existing = self._registrations.get(key)
        registration = PushRegistration(
            room_id=key[0], player_id=player_id, session_identity=identity,
            enabled=enabled,
            registered_at=existing.registered_at if existing else now,
            last_seen=now, _registration_id=registration_id,
        )
        self._registrations[key] = registration
        return registration

    def update_preference(self, room_id: str, player_id: str,
                          session_token: str, enabled: object) -> int:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean.")
        key = self._key(room_id, player_id, session_identity(session_token))
        registration = self._registrations.get(key)
        if registration is None:
            return 0
        registration.enabled = enabled
        registration.last_seen = datetime.now(timezone.utc)
        return 1

    def touch_session(self, room_id: str, player_id: str, session_token: str) -> None:
        key = self._key(room_id, player_id, session_identity(session_token))
        registration = self._registrations.get(key)
        if registration is not None:
            registration.last_seen = datetime.now(timezone.utc)

    def for_player(self, room_id: str, player_id: str,
                   *, enabled_only: bool = False) -> list[PushRegistration]:
        room_id = normalize_room_id(room_id)
        return [registration for registration in self._registrations.values()
                if registration.room_id == room_id
                and hmac.compare_digest(registration.player_id, player_id)
                and (registration.enabled or not enabled_only)]

    def enabled_targets(self, room_id: str, player_id: str) -> list[str]:
        targets: list[str] = []
        seen: set[str] = set()
        for registration in self.for_player(room_id, player_id, enabled_only=True):
            if registration.registration_id not in seen:
                seen.add(registration.registration_id)
                targets.append(registration.registration_id)
        return targets

    def remove_player(self, room_id: str, player_id: str) -> None:
        room_id = normalize_room_id(room_id)
        for key in list(self._registrations):
            if key[0] == room_id and hmac.compare_digest(key[1], player_id):
                del self._registrations[key]

    def remove_room(self, room_id: str) -> None:
        room_id = normalize_room_id(room_id)
        for key in list(self._registrations):
            if key[0] == room_id:
                del self._registrations[key]

    def remove_target(self, registration_id: str) -> None:
        for key, registration in list(self._registrations.items()):
            if hmac.compare_digest(registration.registration_id, registration_id):
                del self._registrations[key]

    def clear(self) -> None:
        self._registrations.clear()

    def __len__(self) -> int:
        return len(self._registrations)
