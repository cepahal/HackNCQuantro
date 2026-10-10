"""Bounded Exa web discovery; results remain unverified candidate pages."""
import ssl
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from ..shared.errors import QuantroError, Stage
from .retrieve_post import _validate_url


class ExaSearch:
    def __init__(self, api_key: str, *, client=None):
        if not api_key.strip():
            raise QuantroError('SEARCH_FAILED', Stage.SEARCH_CANDIDATES,
                               'The source search is not configured.')
        self._key = api_key
        self._client = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=False, verify=ssl.create_default_context())
        self.warnings: list[str] = []

    def close(self):
        self._client.close()

    def find_posts(self, queries, limit):
        self.warnings = ['Exa results are possible sources, not verified matches or established original uploads.']
        selected = queries[:3]
        if len(selected) < len(queries):
            self.warnings.append('Search was bounded to the first three clues plus a video-host search.')
        pages, failed, retryable = [], 0, False
        searches = [(query, None) for query in selected]
        if selected:
            # Preserve open-web coverage and add one recording-focused request.
            # includeDomains is the documented Exa filter (not a site: operator).
            searches.insert(1, (selected[0], ["youtube.com", "vimeo.com", "archive.org"]))
            self.warnings.append('One search targets YouTube, Vimeo and Internet Archive; other recording hosts may be missed.')
        for query, domains in searches:
            try:
                payload = {'query': query, 'type': 'auto', 'numResults': min(limit, 10)}
                if domains is not None:
                    payload['includeDomains'] = domains
                response = self._client.post('https://api.exa.ai/search',
                    headers={'x-api-key': self._key},
                    json=payload)
                response.raise_for_status()
                raw = response.json()['results']
                if not isinstance(raw, list):
                    raise TypeError('Invalid results')
                page = []
                for item in raw[:min(limit, 10)]:
                    try:
                        url, _ = _validate_url(item['url'], allow_webpage=True)
                    except (QuantroError, KeyError, TypeError):
                        self.warnings.append('An invalid or non-public candidate URL was discarded.')
                        continue
                    published = None
                    if isinstance(item.get('publishedDate'), str):
                        try:
                            parsed = datetime.fromisoformat(item['publishedDate'])
                            if parsed.tzinfo is not None:
                                published = parsed.astimezone(UTC).isoformat().replace('+00:00', 'Z')
                        except ValueError:
                            pass
                    page.append(({'post_url': url,
                        'title': item.get('title') if isinstance(item.get('title'), str) else None,
                        'caption': None, 'published_at': published,
                        'author': {'display_name': item.get('author') if isinstance(item.get('author'), str) else None}}, query))
                pages.append(page)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                failed += 1
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                retryable |= isinstance(exc, httpx.TransportError) or status == 429 or (status is not None and status >= 500)
                # Credential/quota failures affect every query: avoid needless calls.
                if status in (401, 402, 403, 429):
                    break
        if not pages:
            raise QuantroError('SEARCH_FAILED', Stage.SEARCH_CANDIDATES,
                               'Exa source search could not be completed.', retryable=retryable)
        if failed:
            self.warnings.append('Some Exa queries failed; search coverage is limited.')
        # Round-robin ranks prevent the first phrase from consuming every slot.
        unique = {}
        order = []
        for rank in range(max(map(len, pages), default=0)):
            for page in pages:
                if rank >= len(page):
                    continue
                post, query = page[rank]
                url = post['post_url']
                if url not in unique:
                    unique[url] = (post, [])
                    order.append(url)
                if query not in unique[url][1]:
                    unique[url][1].append(query)
        # Prefer recording-shaped URLs within the already-fetched results. Keep
        # article pages as fallbacks: they may embed media the worker can extract.
        # This is a retrieval heuristic, never evidence that the source matches.
        def recording_priority(url):
            try:
                _validate_url(url)
                return 0 if not urlsplit(url).path.lower().endswith((".jpg", ".jpeg")) else 1
            except QuantroError:
                return 1
        order.sort(key=recording_priority)
        if any(recording_priority(url) == 0 for url in order):
            self.warnings.append('Recording links were prioritized over general pages for retrieval; source identity still requires alignment.')
        self.warnings = list(dict.fromkeys(self.warnings))
        return [unique[url] for url in order[:limit]]
