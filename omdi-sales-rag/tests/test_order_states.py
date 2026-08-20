from __future__ import annotations

import unittest

from app.services.order_states import (
    CHANGES_REQUESTED,
    COMPANY_APPROVED,
    CUSTOMER_CONFIRMED,
    PENDING_COMPANY_APPROVAL,
    assert_transition,
)


class OrderStateTests(unittest.TestCase):
    def test_company_approval_is_required_before_customer_confirmation(self) -> None:
        with self.assertRaises(ValueError):
            assert_transition(PENDING_COMPANY_APPROVAL, CUSTOMER_CONFIRMED)

        assert_transition(PENDING_COMPANY_APPROVAL, COMPANY_APPROVED)
        assert_transition(COMPANY_APPROVED, CUSTOMER_CONFIRMED)

    def test_changes_can_return_for_company_review(self) -> None:
        assert_transition(PENDING_COMPANY_APPROVAL, CHANGES_REQUESTED)
        assert_transition(CHANGES_REQUESTED, PENDING_COMPANY_APPROVAL)


if __name__ == "__main__":
    unittest.main()

