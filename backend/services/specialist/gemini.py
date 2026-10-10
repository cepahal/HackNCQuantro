"""Private Gemini adapter used only by Jason's specialist stages."""
import json
import logging
import os
import ssl
import time
from pathlib import Path

import httpx

from ..shared.errors import ErrorCode, QuantroError

logger = logging.getLogger(__name__)
_MAX_ATTEMPTS = 3
_FINISH_REASONS = {
    'STOP', 'MAX_TOKENS', 'SAFETY', 'RECITATION', 'BLOCKLIST',
    'PROHIBITED_CONTENT', 'SPII', 'MALFORMED_FUNCTION_CALL', 'OTHER',
}


def _safe_failure(exc):
    status = getattr(exc, 'code', None)
    if type(status) is int and 100 <= status <= 599:
        return f'Gemini request failed (HTTP {status}).'
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return 'Gemini request timed out.'
    if isinstance(exc, (ConnectionError, httpx.NetworkError)):
        return 'Gemini connection failed.'
    return 'Gemini request failed.'


def _retryable(exc):
    return getattr(exc, 'code', None) in (429, 500, 502, 503, 504) or isinstance(
        exc, (TimeoutError, ConnectionError, httpx.TimeoutException, httpx.NetworkError)
    )


def client_and_model(stage):
    from dotenv import load_dotenv
    from google import genai
    root = Path(__file__).resolve().parents[3]
    load_dotenv(root / '.env')
    load_dotenv(root / 'backend' / '.env')
    # Private local settings override the team's tracked placeholders.
    load_dotenv(root / '.env.local', override=True)
    key = os.getenv('GEMINI_API_KEY')
    model = os.getenv('GEMINI_MODEL', 'gemini-flash-latest')
    if not key:
        raise QuantroError(ErrorCode.PROCESSING_FAILED, stage,
                           'GEMINI_API_KEY is not configured.')
    try:
        client = genai.Client(api_key=key, http_options={
            'timeout': 45000,
            # Retries are bounded here, rather than multiplied by SDK retries.
            'retry_options': {'attempts': 1},
            'client_args': {'verify': ssl.create_default_context()},
        })
    except Exception as exc:  # noqa: BLE001 - SDK construction must not expose credentials or connection details.
        raise QuantroError(ErrorCode.PROCESSING_FAILED, stage, _safe_failure(exc), retryable=_retryable(exc)) from None
    return client, model


def generate_json(client, model, contents, schema, stage, code=ErrorCode.PROCESSING_FAILED):
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = client.models.generate_content(
                model=model, contents=contents,
                config={'response_mime_type': 'application/json',
                        'response_json_schema': schema, 'temperature': 0,
                        'max_output_tokens': 8192},
            )
        except Exception as exc:  # noqa: BLE001 - Provider diagnostics may contain credentials or evidence.
            retryable = _retryable(exc)
            message = _safe_failure(exc)
            logger.warning('%s Attempt %d/%d.', message, attempt + 1, _MAX_ATTEMPTS)
            if retryable and attempt + 1 < _MAX_ATTEMPTS:
                time.sleep(2 ** (attempt + 1))
                continue
            raise QuantroError(code, stage, message, retryable=retryable) from None
        if not response.candidates:
            raise QuantroError(code, stage, 'Gemini returned no response candidates.')
        reason = response.candidates[0].finish_reason
        reason = getattr(reason, 'value', reason)
        if reason != 'STOP':
            safe_reason = reason if reason in _FINISH_REASONS else 'UNKNOWN'
            raise QuantroError(code, stage, f'Gemini generation did not complete (finish reason: {safe_reason}).')
        text = response.text
        if not isinstance(text, str) or not text.strip():
            raise QuantroError(code, stage, 'Gemini returned an empty response.')
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            raise QuantroError(code, stage, 'Gemini returned invalid JSON.') from None
