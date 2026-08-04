import os
from mendicot.api.routes import _get_allowed_origins

def test_cors_config_default():
    if "ALLOWED_ORIGINS" in os.environ:
        del os.environ["ALLOWED_ORIGINS"]
    origins = _get_allowed_origins()
    assert origins == ["http://localhost:5173", "http://127.0.0.1:5173"]

def test_cors_config_custom(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.com, https://test.com ")
    origins = _get_allowed_origins()
    assert origins == ["https://example.com", "https://test.com"]

def test_cors_config_empty(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "   ,  ")
    origins = _get_allowed_origins()
    # Should fallback to default if empty
    assert origins == ["http://localhost:5173", "http://127.0.0.1:5173"]

def test_cors_config_deduplicate(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.com,https://example.com")
    origins = _get_allowed_origins()
    assert origins == ["https://example.com"]
