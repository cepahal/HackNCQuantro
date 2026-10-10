"""(conversation_id, job_id) dedupe for the Photon webhook path.

Re-exports the reporting stage's in-memory tracker so both paths share one
implementation: check_and_record is True on the first call for a pair and False
on repeats. Process-local; forgotten on restart. Use separate instances for
inbound dedupe and summary delivery, since a pair recorded by one blocks the other.
"""

from ..reporting.send_summary import InMemoryDeliveryTracker as DeliveryTracker

__all__ = ["DeliveryTracker"]
