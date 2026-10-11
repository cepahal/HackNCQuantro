import pytest


@pytest.fixture(autouse=True)
def _default_search_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing backend.main loads the developer's local vault; tests use the default provider."""
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("SEARCH_FALLBACK_PROVIDER", raising=False)
    monkeypatch.delenv("SEARCH_FALLBACK_API_KEY", raising=False)
