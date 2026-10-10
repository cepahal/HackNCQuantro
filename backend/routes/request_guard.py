"""Compatibility names for the canonical bounded-request middleware."""
from .request_limits import RequestLimitsMiddleware as RequestGuard  # noqa: F401
from .request_limits import WEBHOOK_BODY_LIMIT as MAX_BODY_BYTES  # noqa: F401
