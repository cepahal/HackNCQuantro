import pytest


@pytest.fixture(autouse=True)
def _default_search_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing backend.main loads the developer's local vault; tests use the default provider."""
    # Existing offline tests do not fetch/encode a new source recording.
    # Context integration tests explicitly enable and exercise the new stage.
    monkeypatch.setenv("CONTEXT_VIDEO_ENABLED", "false")
    # Source-search tests enable and mock Groq explicitly; never spend live
    # provider credits because a developer's ignored credentials were loaded.
    monkeypatch.setenv("GROQ_SOURCE_SEARCH", "false")
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("SEARCH_FALLBACK_PROVIDER", raising=False)
    monkeypatch.delenv("SEARCH_FALLBACK_API_KEY", raising=False)
