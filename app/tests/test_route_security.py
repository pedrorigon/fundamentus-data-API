from __future__ import annotations

import pytest

from app.api import routes
from app.config import Settings
from app.core.errors import UnauthorizedCacheInvalidationError


def test_maintenance_operations_fail_closed_without_a_configured_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes, "get_settings", Settings)

    with pytest.raises(UnauthorizedCacheInvalidationError):
        routes._require_maintenance_token(None)
    with pytest.raises(UnauthorizedCacheInvalidationError):
        routes._require_maintenance_token("attacker")


def test_maintenance_token_uses_the_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(cache_invalidate_token="correct-token")
    monkeypatch.setattr(routes, "get_settings", lambda: settings)

    routes._require_maintenance_token("correct-token")
    with pytest.raises(UnauthorizedCacheInvalidationError):
        routes._require_maintenance_token("wrong-token")


def test_force_refresh_requires_maintenance_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes, "get_settings", Settings)

    routes._authorize_force_refresh(False, None)
    with pytest.raises(UnauthorizedCacheInvalidationError):
        routes._authorize_force_refresh(True, None)
