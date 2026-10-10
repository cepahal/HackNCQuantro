"""Pydantic models mirroring the Quantro v2 contract.

Shared by every reporting stage of run_analysis. Only models whose fields have
been confirmed against the contract live here; add the rest as they are
confirmed rather than guessing at fields.
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    NOT_ASSESSED = "not_assessed"


class Outcome(str, Enum):
    MATCHED = "matched"
    NO_VERIFIED_MATCH = "no_verified_match"


class JobCompletionResult(BaseModel):
    job_id: str
    status: Literal["completed"]
    report_id: str
    report_url: str
    completed_at: str


class DeliveryResult(BaseModel):
    conversation_id: str
    message_id: str
    status: Literal["accepted"]
