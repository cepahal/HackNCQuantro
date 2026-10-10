"""Bound JSON bodies before route parsing, including chunked request streams."""

from backend.routes.common import (
    log_failure,
    quantro_error_response,
    request_error_response,
    request_stage,
)
from backend.services.shared.errors import ErrorCode, QuantroError

JSON_BODY_LIMIT = 32 * 1024
WEBHOOK_BODY_LIMIT = 64 * 1024
MAX_JSON_DEPTH = 32


def _too_deep(body: bytes) -> bool:
    depth = 0
    quoted = escaped = False
    for character in body:
        if quoted:
            if escaped:
                escaped = False
            elif character == 92:
                escaped = True
            elif character == 34:
                quoted = False
        elif character == 34:
            quoted = True
        elif character in (91, 123):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return True
        elif character in (93, 125):
            depth -= 1
    return False


class RequestLimitsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def response_send(event):
            nonlocal started
            if event["type"] == "http.response.start":
                started = True
            await send(event)

        try:
            await self._handle(scope, receive, response_send)
        except Exception as error:  # noqa: BLE001 - Prevent raw unexpected exceptions from escaping into server tracebacks.
            safe = error if isinstance(error, QuantroError) else QuantroError(
                ErrorCode.PROCESSING_FAILED, request_stage(scope.get("path", "")))
            log_failure(safe, type(error).__name__)
            if not started:
                await quantro_error_response(safe)(scope, receive, send)
            else:
                # Headers cannot be replaced once a stream starts. Close the
                # interrupted response without exposing private exception text.
                await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _handle(self, scope, receive, send):
        path = scope.get("path", "")
        limit = (WEBHOOK_BODY_LIMIT if path == "/webhooks/photon" else JSON_BODY_LIMIT
                 if path.startswith(("/analyze/", "/ingest/")) else None)
        if scope["type"] != "http" or scope.get("method") != "POST" or limit is None:
            await self.app(scope, receive, send)
            return

        async def reject(status):
            await request_error_response(status, stage=request_stage(path))(scope, receive, send)

        lengths = [value for name, value in scope.get("headers", []) if name.lower() == b"content-length"]
        if lengths:
            if len(lengths) != 1 or not lengths[0].isdigit():
                await reject(400)
                return
            if len(lengths[0]) > 10 or int(lengths[0]) > limit:
                await reject(413)
                return
        chunks = []
        total = 0
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            chunk = event.get("body", b"")
            total += len(chunk)
            if total > limit:
                await reject(413)
                return
            chunks.append(chunk)
            if not event.get("more_body", False):
                break
        body = b"".join(chunks)
        if _too_deep(body):
            await reject(400)
            return
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
