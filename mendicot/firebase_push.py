"""Safe, centralized Firebase Admin initialization and FCM sending."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class FirebaseState(str, Enum):
    CONFIGURED = "configured"
    UNAVAILABLE = "unavailable"
    INITIALIZATION_FAILED = "initialization_failed"


class PushSendResult(str, Enum):
    SENT = "sent"
    UNAVAILABLE = "unavailable"
    INVALID_REGISTRATION = "invalid_registration"
    FAILED = "failed"


@dataclass(frozen=True)
class FirebaseDiagnostic:
    state: FirebaseState
    credential_source: str | None = None


_lock = threading.Lock()
_diagnostic: FirebaseDiagnostic | None = None
_firebase_app = None


def _service_account_info() -> tuple[dict | None, str | None]:
    raw_json = os.getenv("MENDICOT_FIREBASE_SERVICE_ACCOUNT_JSON")
    encoded_json = os.getenv("MENDICOT_FIREBASE_SERVICE_ACCOUNT_JSON_BASE64")
    if raw_json:
        return json.loads(raw_json), "environment_json"
    if encoded_json:
        decoded = base64.b64decode(encoded_json, validate=True).decode("utf-8")
        return json.loads(decoded), "environment_json_base64"
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        return None, "google_application_credentials"
    return None, None


def initialize_firebase() -> FirebaseDiagnostic:
    """Initialize the default Firebase app at most once per process/module load."""
    global _diagnostic, _firebase_app
    if _diagnostic is not None:
        return _diagnostic
    with _lock:
        if _diagnostic is not None:
            return _diagnostic
        try:
            service_account_info, source = _service_account_info()
            if source is None:
                _diagnostic = FirebaseDiagnostic(FirebaseState.UNAVAILABLE)
                return _diagnostic
            import firebase_admin
            from firebase_admin import credentials
            try:
                _firebase_app = firebase_admin.get_app()
            except ValueError:
                credential = (credentials.Certificate(service_account_info)
                              if service_account_info is not None else None)
                _firebase_app = firebase_admin.initialize_app(credential)
            _diagnostic = FirebaseDiagnostic(FirebaseState.CONFIGURED, source)
        except Exception:
            # Deliberately expose no credential content or SDK exception details.
            _firebase_app = None
            _diagnostic = FirebaseDiagnostic(FirebaseState.INITIALIZATION_FAILED)
        return _diagnostic


def get_firebase_diagnostic() -> FirebaseDiagnostic:
    return initialize_firebase()


def _send_turn_notification_sync(registration_id: str) -> PushSendResult:
    diagnostic = initialize_firebase()
    if diagnostic.state != FirebaseState.CONFIGURED or _firebase_app is None:
        return PushSendResult.UNAVAILABLE
    try:
        from firebase_admin import messaging
    except Exception:
        return PushSendResult.FAILED
    try:
        message = messaging.Message(
            token=registration_id,
            data={
                "type": "turn_alert",
                "title": "MendiCot",
                "body": "Your turn in MendiCot",
            },
            webpush=messaging.WebpushConfig(
                headers={"TTL": "300"},
            ),
        )
        messaging.send(message, app=_firebase_app)
        # SENT means FCM accepted the message, not that the OS displayed it.
        return PushSendResult.SENT
    except messaging.UnregisteredError:
        return PushSendResult.INVALID_REGISTRATION
    except Exception:
        return PushSendResult.FAILED


async def send_turn_notification(registration_id: str) -> PushSendResult:
    """Send a data-only turn alert without blocking gameplay."""
    return await asyncio.to_thread(_send_turn_notification_sync, registration_id)


async def send_player_turn_notifications(
    targets: list[str], *, remove_invalid: Callable[[str], None]
) -> list[PushSendResult]:
    results: list[PushSendResult] = []
    for target in dict.fromkeys(targets):
        result = await send_turn_notification(target)
        results.append(result)
        if result == PushSendResult.INVALID_REGISTRATION:
            remove_invalid(target)
    return results


def reset_firebase_for_tests() -> None:
    global _diagnostic, _firebase_app
    with _lock:
        _diagnostic = None
        _firebase_app = None
