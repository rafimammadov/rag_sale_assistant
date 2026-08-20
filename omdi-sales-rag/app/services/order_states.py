from __future__ import annotations


PENDING_COMPANY_APPROVAL = "pending_company_approval"
COMPANY_APPROVED = "company_approved"
CHANGES_REQUESTED = "changes_requested"
REJECTED = "rejected"
CUSTOMER_CONFIRMED = "customer_confirmed"
CANCELLED = "cancelled"

ALLOWED_TRANSITIONS = {
    PENDING_COMPANY_APPROVAL: {COMPANY_APPROVED, CHANGES_REQUESTED, REJECTED, CANCELLED},
    COMPANY_APPROVED: {CUSTOMER_CONFIRMED, CANCELLED},
    CHANGES_REQUESTED: {PENDING_COMPANY_APPROVAL, CANCELLED},
    REJECTED: set(),
    CUSTOMER_CONFIRMED: set(),
    CANCELLED: set(),
}


def assert_transition(current: str, target: str) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"Order cannot move from '{current}' to '{target}'.")

