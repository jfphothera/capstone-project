"""Request bodies. Validation lives here so handlers stay short.

Every body that causes a consequential change carries an `authorizer`, and it
is required rather than defaulted: the record of who approved something is
part of the approval, and a request that cannot say who is making it is
rejected before it reaches the business rules.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Authorised(BaseModel):
    authorizer: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=4000)

    @field_validator('authorizer')
    @classmethod
    def named_person(cls, value):
        if not value.strip():
            raise ValueError('A named representative is required')
        return value.strip()


class PriorityOverride(Authorised):
    priority: Literal["Critical", "High", "Medium", "Low"]
    reason: str = Field(min_length=3, max_length=2000)


class Assignment(Authorised):
    assignee: str = Field(min_length=1, max_length=120)


class ItemDecision(Authorised):
    decision: Literal["approved", "rejected"]
    feedback: str = Field(default="", max_length=4000)


class RevisionConfirmation(Authorised):
    decision: Literal["confirmed", "rejected"]


class SendReply(Authorised):
    body: str = Field(min_length=1, max_length=20000)
    review_override: str = Field(default='', max_length=4000)


class Regenerate(Authorised):
    feedback: str = Field(min_length=3, max_length=4000)


class CloseCase(Authorised):
    resolved: bool = True
    resolution: str = Field(default="", max_length=2000)


class Determination(Authorised):
    determination: Literal['confirmed adverse event', 'suspected adverse event',
                           'device hazard without confirmed injury',
                           'not an adverse event', 'insufficient information']
    rationale: str = Field(min_length=3, max_length=2000)


class ShipmentAdvance(BaseModel):
    status: Literal['label_created', 'pickup_scheduled', 'pickup_attempt_failed', 'picked_up',
                    'drop_off_pending', 'carrier_accepted', 'in_transit', 'delivered', 'failed']
    location: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=500)


class ProposalReview(Authorised):
    decision: Literal["approved", "rejected"]


class MovementApproval(Authorised):
    simulate_failure: bool = False


class ReturnMethod(Authorised):
    method: Literal['carrier_pickup', 'drop_off_label']


class SimulatedReply(Authorised):
    text: str = Field(min_length=1, max_length=4000)
    parent_message_id: str = Field(min_length=1, max_length=120)
    continue_investigation: bool = True   # rc.27: False = record the reply only (no queued investigation pass)


class RepresentativeFeedback(Authorised):
    target_id: str = Field(min_length=1, max_length=120)
    category: Literal['classification', 'priority', 'qms_source', 'wording', 'identity_device',
                      'unnecessary_escalation', 'movement_shipping', 'other']
    correction: str = Field(min_length=3, max_length=4000)
    explanation: str = Field(min_length=3, max_length=4000)


class ExchangeProposal(Authorised):
    note: str = Field(min_length=3, max_length=2000)


class ChatMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class TicketIn(BaseModel):
    """A ticket typed in by hand, for demonstrations and acceptance tests."""
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20000)
    account_id: Optional[str] = None
    channel: Literal["email", "chat", "phone"] = "email"
    author: Optional[str] = None


class ScenarioRun(BaseModel):
    scenario_id: Optional[str] = None
    category: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=1000)
    seed: int = 7
    out_dir: Optional[str] = None
