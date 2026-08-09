import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mendicot import firebase_push
from mendicot.api import routes
from mendicot.firebase_push import (
    FirebaseDiagnostic,
    FirebaseState,
    PushSendResult,
)
from mendicot.push_registrations import (
    MAX_REGISTRATION_ID_LENGTH,
    PushRegistrationStore,
)


client = TestClient(routes.app)


@pytest.fixture(autouse=True)
def reset_push_and_room_state():
    for task in list(routes._disconnect_cleanup_tasks.values()):
        task.cancel()
    routes._disconnect_cleanup_tasks.clear()
    routes.room_manager._rooms.clear()
    routes.connection_manager.active_connections.clear()
    routes.session_tokens.clear()
    routes.invalidated_session_tokens.clear()
    routes.push_registrations.clear()
    firebase_push.reset_firebase_for_tests()
    yield
    for task in list(routes._disconnect_cleanup_tasks.values()):
        task.cancel()
    routes._disconnect_cleanup_tasks.clear()
    routes.room_manager._rooms.clear()
    routes.connection_manager.active_connections.clear()
    routes.session_tokens.clear()
    routes.invalidated_session_tokens.clear()
    routes.push_registrations.clear()
    firebase_push.reset_firebase_for_tests()


def _clear_firebase_environment(monkeypatch):
    for name in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "MENDICOT_FIREBASE_SERVICE_ACCOUNT_JSON",
        "MENDICOT_FIREBASE_SERVICE_ACCOUNT_JSON_BASE64",
    ):
        monkeypatch.delenv(name, raising=False)


def _fake_firebase_modules(monkeypatch, *, existing_app=None, initialize_error=None):
    calls = {"get_app": 0, "initialize_app": 0, "certificate": 0}
    module = ModuleType("firebase_admin")

    def get_app():
        calls["get_app"] += 1
        if existing_app is None:
            raise ValueError("no default app")
        return existing_app

    def initialize_app(credential):
        calls["initialize_app"] += 1
        if initialize_error:
            raise initialize_error
        return SimpleNamespace(credential=credential)

    def certificate(info):
        calls["certificate"] += 1
        return SimpleNamespace(info=info)

    module.get_app = get_app
    module.initialize_app = initialize_app
    module.credentials = SimpleNamespace(Certificate=certificate)
    monkeypatch.setitem(sys.modules, "firebase_admin", module)
    return calls


def test_missing_firebase_configuration_is_safe(monkeypatch):
    _clear_firebase_environment(monkeypatch)
    diagnostic = firebase_push.initialize_firebase()
    assert diagnostic == FirebaseDiagnostic(FirebaseState.UNAVAILABLE)


def test_firebase_configured_initializes_once(monkeypatch):
    _clear_firebase_environment(monkeypatch)
    monkeypatch.setenv(
        "MENDICOT_FIREBASE_SERVICE_ACCOUNT_JSON",
        '{"project_id":"project","private_key":"not-logged"}',
    )
    calls = _fake_firebase_modules(monkeypatch)

    first = firebase_push.initialize_firebase()
    second = firebase_push.initialize_firebase()

    assert first.state == FirebaseState.CONFIGURED
    assert first.credential_source == "environment_json"
    assert second is first
    assert calls == {"get_app": 1, "initialize_app": 1, "certificate": 1}


def test_firebase_reuses_existing_default_app(monkeypatch):
    _clear_firebase_environment(monkeypatch)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "configured-path.json")
    existing = object()
    calls = _fake_firebase_modules(monkeypatch, existing_app=existing)

    assert firebase_push.initialize_firebase().state == FirebaseState.CONFIGURED
    assert calls["get_app"] == 1
    assert calls["initialize_app"] == 0


def test_firebase_initialization_failure_is_safe(monkeypatch):
    _clear_firebase_environment(monkeypatch)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "configured-path.json")
    _fake_firebase_modules(monkeypatch, initialize_error=RuntimeError("secret detail"))

    diagnostic = firebase_push.initialize_firebase()

    assert diagnostic == FirebaseDiagnostic(FirebaseState.INITIALIZATION_FAILED)
    assert "secret" not in repr(diagnostic)


def test_push_health_exposes_only_safe_diagnostics(monkeypatch):
    _clear_firebase_environment(monkeypatch)
    response = client.get("/health/push")
    assert response.status_code == 200
    assert response.json() == {"status": "unavailable", "credential_source": None}


def test_registration_validation_and_token_redacted_repr():
    store = PushRegistrationStore()
    with pytest.raises(ValueError):
        store.register("room", "P1", "session", "", True)
    with pytest.raises(ValueError):
        store.register("room", "P1", "session", "x" * (MAX_REGISTRATION_ID_LENGTH + 1), True)
    with pytest.raises(ValueError):
        store.register("room", "P1", "session", "target", "true")

    registration = store.register("room", "P1", "session", "private-target", True)
    assert "private-target" not in repr(registration)


def test_shared_target_preserves_session_preferences_and_identity():
    store = PushRegistrationStore()
    store.register("room", "P1", "session-1", "shared-target", False)
    store.register("room", "P2", "session-2", "shared-target", True)

    assert store.enabled_targets("room", "P1") == []
    assert store.enabled_targets("room", "P2") == ["shared-target"]
    assert len(store) == 2


def test_target_selection_deduplicates_only_within_authoritative_player():
    store = PushRegistrationStore()
    store.register("room", "P1", "session-1", "shared-target", True)
    store.register("room", "P1", "session-2", "shared-target", True)
    store.register("room", "P2", "session-3", "shared-target", True)

    assert store.enabled_targets("room", "P1") == ["shared-target"]
    assert store.enabled_targets("room", "P2") == ["shared-target"]


def _configure_fake_sender(monkeypatch, send_impl):
    class UnregisteredError(Exception):
        pass

    messaging = SimpleNamespace()
    messaging.Notification = lambda **values: SimpleNamespace(**values)
    messaging.WebpushNotification = lambda **values: SimpleNamespace(**values)
    messaging.WebpushConfig = lambda **values: SimpleNamespace(**values)
    messaging.Message = lambda **values: SimpleNamespace(**values)
    messaging.send = send_impl
    messaging.UnregisteredError = UnregisteredError
    admin_module = ModuleType("firebase_admin")
    admin_module.messaging = messaging
    monkeypatch.setitem(sys.modules, "firebase_admin", admin_module)
    firebase_push._diagnostic = FirebaseDiagnostic(FirebaseState.CONFIGURED, "test")
    firebase_push._firebase_app = object()
    return UnregisteredError


def test_sender_uses_data_only_turn_alert_payload(monkeypatch):
    sent = []
    _configure_fake_sender(monkeypatch, lambda message, app: sent.append((message, app)))
    messaging = sys.modules["firebase_admin"].messaging

    def unexpected_notification(**values):
        pytest.fail(f"Unexpected Firebase notification payload: {values!r}")

    messaging.Notification = unexpected_notification
    messaging.WebpushNotification = unexpected_notification

    result = firebase_push._send_turn_notification_sync("private-target")

    assert result == PushSendResult.SENT
    message, app = sent[0]
    assert message.token == "private-target"
    assert message.data == {
        "type": "turn_alert",
        "title": "MendiCot",
        "body": "Your turn in MendiCot",
    }
    assert all(isinstance(value, str) for value in message.data.values())
    assert not hasattr(message, "notification")
    assert message.webpush.headers == {"TTL": "300"}
    assert not hasattr(message.webpush, "notification")
    assert vars(message).keys() == {"token", "data", "webpush"}
    assert app is firebase_push._firebase_app


def test_sender_reports_unregistered_and_transient_errors(monkeypatch):
    unregistered = _configure_fake_sender(monkeypatch, lambda message, app: None)

    def invalid_send(message, app):
        raise unregistered()

    sys.modules["firebase_admin"].messaging.send = invalid_send
    assert (
        firebase_push._send_turn_notification_sync("invalid")
        == PushSendResult.INVALID_REGISTRATION
    )

    sys.modules["firebase_admin"].messaging.send = lambda message, app: (_ for _ in ()).throw(
        ConnectionError("temporary")
    )
    assert firebase_push._send_turn_notification_sync("valid") == PushSendResult.FAILED


def test_send_batch_cleans_permanent_target_but_keeps_transient(monkeypatch):
    store = PushRegistrationStore()
    store.register("room", "P1", "s1", "invalid", True)
    store.register("room", "P1", "s2", "transient", True)

    async def fake_send(target):
        return (PushSendResult.INVALID_REGISTRATION
                if target == "invalid" else PushSendResult.FAILED)

    monkeypatch.setattr(firebase_push, "send_turn_notification", fake_send)
    results = asyncio.run(firebase_push.send_player_turn_notifications(
        ["invalid", "transient"], remove_invalid=store.remove_target
    ))

    assert results == [PushSendResult.INVALID_REGISTRATION, PushSendResult.FAILED]
    assert [item.registration_id for item in store.for_player("room", "P1")] == [
        "transient"
    ]


def _create_and_join(player_id="P1"):
    room_id = client.post(
        "/api/rooms", json={"player_count": 4, "trump_mode": "normal"}
    ).json()["room_id"]
    player = client.post(
        f"/api/rooms/{room_id}/join",
        json={"player_id": player_id, "display_name": player_id},
    ).json()
    return room_id, player


def _receive_type(websocket, message_type, max_messages=20):
    for _ in range(max_messages):
        message = websocket.receive_json()
        if message["type"] == message_type:
            return message
    raise AssertionError(f"Did not receive {message_type}")


def _register(websocket, target="target", enabled=True):
    websocket.send_json({
        "action": "REGISTER_PUSH",
        "payload": {"registration_id": target, "enabled": enabled},
    })
    return _receive_type(websocket, "ACTION_SUCCESS")


def test_valid_socket_registers_for_authoritative_player_without_broadcasting_target():
    room_id, player = _create_and_join()
    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={player['session_token']}"
    ) as websocket:
        initial = websocket.receive_json()
        success = _register(websocket, "private-target")

    assert success["payload"] == {"action": "REGISTER_PUSH", "enabled": True}
    registrations = routes.push_registrations.for_player(room_id, "P1")
    assert len(registrations) == 1
    assert registrations[0].registration_id == "private-target"
    assert "private-target" not in str(initial)
    assert "registration" not in str(initial).lower()


@pytest.mark.parametrize("registration_id", ["", "   ", "x" * 4097])
def test_socket_rejects_malformed_registration(registration_id):
    room_id, player = _create_and_join()
    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={player['session_token']}"
    ) as websocket:
        websocket.receive_json()
        websocket.send_json({
            "action": "REGISTER_PUSH",
            "payload": {"registration_id": registration_id, "enabled": True},
        })
        error = _receive_type(websocket, "ERROR")
    assert error["payload"]["code"] == "INVALID_PAYLOAD"
    assert len(routes.push_registrations) == 0


def test_client_cannot_supply_player_identity_for_registration():
    room_id, player = _create_and_join("P1")
    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={player['session_token']}"
    ) as websocket:
        websocket.receive_json()
        websocket.send_json({
            "action": "REGISTER_PUSH",
            "payload": {
                "registration_id": "target", "enabled": True, "player_id": "P2"
            },
        })
        error = _receive_type(websocket, "ERROR")
    assert error["payload"]["code"] == "INVALID_PAYLOAD"
    assert routes.push_registrations.for_player(room_id, "P2") == []


def test_push_actions_require_current_authoritative_socket():
    room_id, player = _create_and_join("P1")
    current_socket = object()
    stale_socket = object()
    routes.connection_manager.active_connections[room_id] = {
        "P1": current_socket
    }
    room = routes.room_manager.get_room(room_id)

    assert routes._push_session_is_active(
        room_id, "P1", player["session_token"], current_socket, room
    ) is True
    assert routes._push_session_is_active(
        room_id, "P1", player["session_token"], stale_socket, room
    ) is False


def test_preference_off_is_retained_for_current_session():
    room_id, player = _create_and_join()
    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={player['session_token']}"
    ) as websocket:
        websocket.receive_json()
        _register(websocket)
        websocket.send_json({
            "action": "UPDATE_PUSH_PREFERENCE", "payload": {"enabled": False}
        })
        success = _receive_type(websocket, "ACTION_SUCCESS")

    assert success["payload"]["status"] == "updated"
    assert routes.push_registrations.enabled_targets(room_id, "P1") == []
    assert routes.push_registrations.for_player(room_id, "P1")[0].enabled is False


def test_disconnect_and_reconnect_retain_registration():
    room_id, player = _create_and_join()
    url = f"/ws/rooms/{room_id}?token={player['session_token']}"
    with client.websocket_connect(url) as websocket:
        websocket.receive_json()
        _register(websocket, "retained-target")

    assert routes.push_registrations.enabled_targets(room_id, "P1") == [
        "retained-target"
    ]
    with client.websocket_connect(url) as replacement:
        replacement.receive_json()
        assert routes.push_registrations.enabled_targets(room_id, "P1") == [
            "retained-target"
        ]


def test_permanent_timeout_removal_cleans_registration(monkeypatch):
    room_id, departing = _create_and_join("departing")
    client.post(
        f"/api/rooms/{room_id}/join",
        json={"player_id": "remaining", "display_name": "remaining"},
    )
    routes.push_registrations.register(
        room_id, "departing", departing["session_token"], "target", True
    )
    monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0.001)

    class Socket:
        async def accept(self):
            pass

        async def close(self, **kwargs):
            pass

        async def send_json(self, message):
            pass

    async def expire():
        socket = Socket()
        await routes.connection_manager.connect(room_id, "departing", socket)
        await routes._handle_socket_disconnect(room_id, "departing", socket)
        await asyncio.sleep(0.02)

    asyncio.run(expire())
    assert routes.push_registrations.for_player(room_id, "departing") == []


def test_leave_room_and_room_deletion_clean_registrations():
    room_id, player = _create_and_join()
    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={player['session_token']}"
    ) as websocket:
        websocket.receive_json()
        _register(websocket, "leave-target")
        websocket.send_json({"action": "LEAVE_ROOM", "payload": {}})
        _receive_type(websocket, "ACTION_SUCCESS")
    assert routes.push_registrations.for_player(room_id, "P1") == []

    other_room, other = _create_and_join("P2")
    routes.push_registrations.register(
        other_room, "P2", other["session_token"], "room-target", True
    )
    routes.room_manager.delete_room(other_room)
    assert routes.push_registrations.for_player(other_room, "P2") == []


def test_return_to_lobby_action_preserves_registration(monkeypatch):
    room_id, player = _create_and_join()
    room = routes.room_manager.get_room(room_id)
    monkeypatch.setattr(room, "return_to_lobby", lambda player_id: False)
    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={player['session_token']}"
    ) as websocket:
        websocket.receive_json()
        _register(websocket, "preserved-target")
        websocket.send_json({"action": "RETURN_TO_LOBBY", "payload": {}})
        _receive_type(websocket, "ACTION_SUCCESS")
    assert routes.push_registrations.enabled_targets(room_id, "P1") == [
        "preserved-target"
    ]


def test_test_push_is_production_gated(monkeypatch):
    room_id, player = _create_and_join()
    monkeypatch.setenv("MENDICOT_ENV", "production")
    monkeypatch.setenv("MENDICOT_ENABLE_TEST_PUSH", "true")
    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={player['session_token']}"
    ) as websocket:
        websocket.receive_json()
        _register(websocket)
        websocket.send_json({"action": "TEST_PUSH_NOTIFICATION", "payload": {}})
        success = _receive_type(websocket, "ACTION_SUCCESS")
    assert success["payload"] == {
        "action": "TEST_PUSH_NOTIFICATION", "status": "unavailable"
    }


def test_test_push_reports_no_registration_in_development(monkeypatch):
    room_id, player = _create_and_join()
    monkeypatch.setenv("MENDICOT_ENV", "development")
    monkeypatch.setenv("MENDICOT_ENABLE_TEST_PUSH", "true")
    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={player['session_token']}"
    ) as websocket:
        websocket.receive_json()
        websocket.send_json({"action": "TEST_PUSH_NOTIFICATION", "payload": {}})
        success = _receive_type(websocket, "ACTION_SUCCESS")
    assert success["payload"]["status"] == "no_registration"


@pytest.mark.parametrize(
    ("send_results", "expected"),
    [
        ([PushSendResult.SENT], "sent"),
        ([PushSendResult.UNAVAILABLE], "unavailable"),
        ([PushSendResult.FAILED], "failed"),
    ],
)
def test_test_push_uses_turn_notification_sender_for_requesting_player(
    monkeypatch, send_results, expected
):
    room_id, player = _create_and_join("P1")
    second = client.post(
        f"/api/rooms/{room_id}/join",
        json={"player_id": "P2", "display_name": "P2"},
    ).json()
    routes.push_registrations.register(
        room_id, "P2", second["session_token"], "other-target", True
    )
    captured = []

    async def fake_send(targets, *, remove_invalid):
        captured.extend(targets)
        return send_results

    monkeypatch.setattr(firebase_push, "send_player_turn_notifications", fake_send)
    monkeypatch.setenv("MENDICOT_ENV", "test")
    monkeypatch.setenv("MENDICOT_ENABLE_TEST_PUSH", "true")
    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={player['session_token']}"
    ) as websocket:
        websocket.receive_json()
        _register(websocket, "own-target")
        websocket.send_json({"action": "TEST_PUSH_NOTIFICATION", "payload": {}})
        success = _receive_type(websocket, "ACTION_SUCCESS")

    assert captured == ["own-target"]
    assert success["payload"]["status"] == expected


def test_test_push_skips_disabled_registration(monkeypatch):
    room_id, player = _create_and_join("P1")
    send_called = False

    async def fake_send(targets, *, remove_invalid):
        nonlocal send_called
        send_called = True
        return [PushSendResult.SENT]

    monkeypatch.setattr(firebase_push, "send_player_turn_notifications", fake_send)
    monkeypatch.setenv("MENDICOT_ENV", "test")
    monkeypatch.setenv("MENDICOT_ENABLE_TEST_PUSH", "true")
    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={player['session_token']}"
    ) as websocket:
        websocket.receive_json()
        _register(websocket, "disabled-target", enabled=False)
        websocket.send_json({"action": "TEST_PUSH_NOTIFICATION", "payload": {}})
        success = _receive_type(websocket, "ACTION_SUCCESS")

    assert send_called is False
    assert success["payload"]["status"] == "no_registration"


def test_test_push_rejects_arbitrary_target_payload(monkeypatch):
    room_id, player = _create_and_join()
    monkeypatch.setenv("MENDICOT_ENV", "test")
    monkeypatch.setenv("MENDICOT_ENABLE_TEST_PUSH", "true")
    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={player['session_token']}"
    ) as websocket:
        websocket.receive_json()
        _register(websocket)
        websocket.send_json({
            "action": "TEST_PUSH_NOTIFICATION",
            "payload": {"registration_id": "arbitrary-target"},
        })
        error = _receive_type(websocket, "ERROR")
    assert error["payload"]["code"] == "INVALID_PAYLOAD"
