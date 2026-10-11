"""Bounded Exa web discovery; results remain unverified candidate pages."""
import logging
import re
import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from ..shared.errors import QuantroError, Stage
from ..shared.post_identity import post_identity
from .retrieve_post import _validate_url

logger = logging.getLogger(__name__)
MAX_PUBLISHER_LINKS = 10


def _publisher_youtube_url(value):
    """Canonicalize returned public links without fetching their destinations."""
    if not isinstance(value, str) or len(value) > 2_048:
        return None
    try:
        value, _ = _validate_url(value, allow_webpage=True)
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        if parsed.port not in {None, 443 if parsed.scheme == 'https' else 80}:
            return None
        if host in {'youtube-nocookie.com', 'www.youtube-nocookie.com'}:
            match = re.fullmatch(r'/embed/([A-Za-z0-9_-]{11})/?', parsed.path)
            identity = 'youtube:' + match[1] if match else ''
        elif host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be'}:
            identity = post_identity(value)
        else:
            return None
        if re.fullmatch(r'youtube:[A-Za-z0-9_-]{11}', identity):
            return 'https://www.youtube.com/watch?v=' + identity.removeprefix('youtube:')
    except (QuantroError, ValueError, TypeError):
        pass
    return None


class ExaSearch:
    def __init__(self, api_key: str, *, client=None, excluded_identities=()):
        if not api_key.strip():
            logger.error("EXA_API_KEY is required when SEARCH_PROVIDER=exa.")
            raise QuantroError('SEARCH_FAILED', Stage.SEARCH_CANDIDATES,
                               'The source search is not configured.')
        self._excluded = frozenset(excluded_identities)
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
        def fetch(search):
            query, domains = search
            warnings = []
            try:
                payload = {'query': query, 'type': 'auto', 'numResults': min(limit * 4, 10),
                           'contents': {'extras': {'links': MAX_PUBLISHER_LINKS}}}
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
                for item in raw[:10]:
                    try:
                        url, _ = _validate_url(item['url'], allow_webpage=True)
                    except (QuantroError, KeyError, TypeError):
                        warnings.append('An invalid or non-public candidate URL was discarded.')
                        continue
                    if post_identity(url) in self._excluded:
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
                        'author': {'display_name': item.get('author') if isinstance(item.get('author'), str) else None}}, query, False))
                    # Video-page outbound links commonly contain unrelated
                    # recommendations, not a publisher's event recording.
                    if _publisher_youtube_url(url) is not None:
                        continue
                    extras = item.get('extras')
                    links = extras.get('links') if isinstance(extras, dict) else None
                    if not isinstance(links, list):
                        continue
                    for link in links[:MAX_PUBLISHER_LINKS]:
                        recording = _publisher_youtube_url(link)
                        if recording is None or post_identity(recording) in self._excluded:
                            continue
                        # A page link is discovery provenance, never the video's
                        # publication date, author, caption or verified title.
                        title = item.get('title')
                        title = 'Publisher-page title clue: ' + ' '.join(title[:1_000].split())[:180] if isinstance(title, str) and title.strip() else None
                        page_clue = url[:350] + (' [truncated]' if len(url) > 350 else '')
                        if any(marker in page_clue for marker in ('/tmp/', 'file://')):
                            page_clue = 'URL omitted from display'
                        provenance = f'Publisher page: {page_clue}. Linked recording; this page association is unverified.'
                        page.append(({'post_url': recording, 'title': title, 'caption': provenance,
                                      'published_at': None, 'author': {}}, query, True))
                        warnings.append('Publisher-linked YouTube URLs are unverified discovery leads; a page title, author and publication date do not establish recording metadata or originality.')
                        warnings.append(provenance)
                return page, warnings, None
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                return [], warnings, exc
        # Executor.map preserves clue order despite different completion times.
        # The shared synchronous httpx client supports concurrent requests.
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="exa-search") as executor:
            outcomes = list(executor.map(fetch, searches))
        for page, warnings, exc in outcomes:
            self.warnings.extend(warnings)
            if exc is None:
                pages.append(page)
            else:
                failed += 1
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                retryable |= isinstance(exc, httpx.TransportError) or status == 429 or (status is not None and status >= 500)
        if not pages or (failed and not any(pages)):
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
                post, query, publisher_linked = page[rank]
                url = post['post_url']
                identity = post_identity(url)
                if identity not in unique:
                    unique[identity] = (post, [], publisher_linked)
                    order.append(identity)
                elif unique[identity][2] and not publisher_linked:
                    # An actual recording search result takes metadata priority
                    # over a referring publisher page, regardless of rank.
                    _, matched, _ = unique[identity]
                    unique[identity] = (post, matched, False)
                if query not in unique[identity][1]:
                    unique[identity][1].append(query)
        # Prefer recording-shaped URLs within the already-fetched results. Keep
        # article pages as fallbacks: they may embed media the worker can extract.
        # This is a retrieval heuristic, never evidence that the source matches.
        def recording_priority(url):
            try:
                _validate_url(url)
                return 0 if not urlsplit(url).path.lower().endswith((".jpg", ".jpeg")) else 1
            except QuantroError:
                return 1
        order.sort(key=lambda identity: recording_priority(unique[identity][0]["post_url"]))
        if any(recording_priority(unique[identity][0]["post_url"]) == 0 for identity in order):
            self.warnings.append('Recording links were prioritized over general pages for retrieval; source identity still requires alignment.')
        self.warnings = list(dict.fromkeys(self.warnings))
        return [(unique[url][0], unique[url][1]) for url in order[:limit]]
