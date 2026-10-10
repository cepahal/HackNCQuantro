"""ASGI guard: bounded request bodies and an enveloped 500 for unexpected exceptions."""

import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.routes.common import error_response, stage_for_path
from backend.services.shared.errors import ErrorCode

logger = logging.getLogger(__name__)

# Every JSON route takes a URL (<= 8192 chars) and small IDs; Photon webhooks are capped here too.
MAX_BODY_BYTES = 64 * 1024


class _BodyTooLarge(Exception):
    pass


class RequestGuard:
    def __init__(self, app: ASGIApp, max_body_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        stage = stage_for_path(scope["path"])
        if scope["method"] in {"POST", "PUT", "PATCH"}:
            try:
                body = await self._read_body(scope, receive)
            except _BodyTooLarge:
                await error_response(413, ErrorCode.PROCESSING_FAILED, stage, "The request body is too large.")(scope, receive, send)
                return
            except ValueError:
                await error_response(400, ErrorCode.PROCESSING_FAILED, stage, "The request body could not be read.")(scope, receive, send)
                return
            receive = _replay(body, receive)

        response_started = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception as error:
            logger.exception("Unhandled %s on %s %s", type(error).__name__, scope["method"], scope["path"])
            if response_started:
                raise
            await error_response(500, ErrorCode.PROCESSING_FAILED, stage)(scope, receive, send)

    async def _read_body(self, scope: Scope, receive: Receive) -> bytes:
        for name, value in scope["headers"]:
            if name == b"content-length":
                if not value.isdigit():
                    raise ValueError("invalid Content-Length")
                if int(value) > self.max_body_bytes:
                    raise _BodyTooLarge
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise ValueError("client disconnected")
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body_bytes:
                raise _BodyTooLarge
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if delivered:
            return await receive()
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay
