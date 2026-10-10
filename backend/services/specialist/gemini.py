"""Private Gemini adapter used only by Jason's specialist stages."""
import logging
import os
import ssl

import httpx

from ..shared.errors import ErrorCode, QuantroError
from ..shared.settings import load_environment

logger = logging.getLogger(__name__)


def _retryable(exc):
    return getattr(exc, 'code', None) in (429, 500, 502, 503, 504) or isinstance(
        exc, (TimeoutError, ConnectionError, httpx.TimeoutException, httpx.NetworkError)
    )


def load_settings():
    load_environment()


def client_and_model(stage):
    from google import genai
    load_settings()
    key = os.getenv('GEMINI_API_KEY')
    model = os.getenv('GEMINI_MODEL', 'gemini-flash-latest')
    if not key:
        logger.error('GEMINI_API_KEY is not configured.')
        raise QuantroError(ErrorCode.PROCESSING_FAILED, stage)
    try:
        client = genai.Client(api_key=key, http_options={
            'timeout': 120000,
            'client_args': {'verify': ssl.create_default_context()},
        })
    except Exception as exc:  # noqa: BLE001 - SDK construction must not expose credentials or connection details.
        raise QuantroError(ErrorCode.PROCESSING_FAILED, stage, retryable=_retryable(exc)) from None
    return client, model


def generate_json(client, model, contents, schema, stage, code=ErrorCode.PROCESSING_FAILED):
    import json
    try:
        response = client.models.generate_content(
            model=model, contents=contents,
            config={'response_mime_type': 'application/json',
                    'response_json_schema': schema, 'temperature': 0},
        )
        if not response.candidates:
            raise ValueError('No response candidate')
        reason = response.candidates[0].finish_reason
        if getattr(reason, 'value', reason) != 'STOP':
            raise ValueError('Generation did not complete')
        return json.loads(response.text)
    except Exception as exc:  # noqa: BLE001 - Normalize provider and transport failures without diagnostics.
        raise QuantroError(code, stage, retryable=_retryable(exc)) from None
