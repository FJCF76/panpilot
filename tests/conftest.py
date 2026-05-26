"""
Shared test fixtures.

- test_env: autouse — sets all required env vars to safe test values, DRY_RUN=true,
  and clears the get_settings() lru_cache so each test gets a fresh Settings instance.
  Tests that need live-mode or custom settings monkeypatch individual vars after this runs.
- mock_anthropic: patches anthropic.Anthropic so no real API calls are made.
- mock_proactivanet: uses respx to intercept all outbound httpx requests.
"""
import pytest
import respx
from unittest.mock import MagicMock, patch

from panpilot.config import get_settings

_TEST_ENV = {
    "PROACTIVANET_API_URL": "https://test.proactivanet.example/api",
    "PROACTIVANET_BASE_URL": "https://test.proactivanet.example",
    "PROACTIVANET_API_KEY": "test-api-key",
    "PROACTIVANET_AUTHOR_ID": "00000000-0000-0000-0000-000000000001",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "test-admin-password",
    "DRY_RUN": "true",
    "DATA_DIR": "/tmp/panpilot-test-data",
}


@pytest.fixture(autouse=True)
def test_env(monkeypatch):
    """Apply safe test environment for every test and reset the settings cache."""
    for key, value in _TEST_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _mock_translation(monkeypatch):
    """Replace _translate_to_spanish with identity for all tests.

    Prevents real Anthropic API calls from the audit writer in every test that
    exercises route() or write_audit(). Tests that specifically test translation
    behaviour override this by calling monkeypatch.setattr again in their body.
    """
    monkeypatch.setattr(
        "panpilot.execution.audit._translate_to_spanish",
        lambda text, client: text,
    )


@pytest.fixture
def mock_anthropic():
    """Patch the Anthropic client to return a controllable mock."""
    with patch("anthropic.Anthropic") as mock_cls:
        client = MagicMock()
        mock_cls.return_value = client
        yield client


@pytest.fixture
def mock_proactivanet():
    """Intercept all outbound httpx requests (Proactivanet API calls) via respx.
    Tests add routes: mock_proactivanet.get("/api/Incidents/...").mock(...)
    """
    with respx.mock(assert_all_called=False) as router:
        yield router
