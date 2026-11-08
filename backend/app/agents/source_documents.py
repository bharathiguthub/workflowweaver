"""Synthetic Google Drive SOURCE documents for the Phase 7.2 orchestration workflow.

Used in mock mode (the mock Drive adapter serves them by file id) and in dry-run planning (dry
run never contacts Google Drive, so the source is simulated and labelled as such). All content
is synthetic. Two files deliberately share a name, so selection is always by file id and an
ambiguous name is resolved explicitly in the UI, never guessed.
"""

import hashlib
from dataclasses import dataclass

SOURCE_MIME = "text/markdown"

CUSTOMER_REWARDS_PLAN = """# Customer Rewards Platform - Implementation Plan

Synthetic planning document for WorkflowWeaver demonstrations. It contains no real data.

## Summary
The Customer Rewards Platform lets customers register, earn points on purchases, check their
reward balance, redeem points for rewards and review their transaction history. Delivery is
split into five implementation phases.

## Phase 1: Customer registration
Priority: High
Owner: Identity team

### Objectives
- Let customers create a rewards account with email verification
- Link a rewards account to an existing customer profile

### Technical tasks
- Build the registration API with input validation
- Send verification emails through the notification service
- Store accounts in the rewards database with encrypted contact details

### Deliverables
- Registration API and verification flow
- Database schema for rewards accounts

### Acceptance criteria
- A new customer can register and verify their email address
- Duplicate registrations for the same email are rejected

## Phase 2: Reward balance lookup
Priority: High

### Objectives
- Show customers their current points balance in real time

### Technical tasks
- Build the balance API backed by the points ledger
- Add a balance cache with a five minute expiry

### Deliverables
- Balance API and cache layer

### Acceptance criteria
- The balance shown matches the points ledger
- Balance requests respond in under 300 milliseconds at the 95th percentile

## Phase 3: Points earning
Owner: Payments team

### Objectives
- Award points automatically for eligible purchases

### Technical tasks
- Consume purchase events from the order service
- Apply earning rules per product category
- Record every award in the points ledger

### Deliverables
- Earning rules engine
- Points ledger writer

### Acceptance criteria
- Eligible purchases award the configured points exactly once
- Refunded purchases reverse the awarded points

## Phase 4: Points redemption
Priority: Medium
Owner: Payments team

### Objectives
- Let customers redeem points for rewards from a catalog

### Technical tasks
- Build the redemption API with balance checks
- Reserve points during checkout and release them on cancellation

### Deliverables
- Redemption API
- Reward catalog service

### Acceptance criteria
- A redemption never makes a balance negative
- Cancelled redemptions return the reserved points

## Phase 5: Transaction history
Priority: Low

### Objectives
- Give customers a searchable history of earned and redeemed points

### Technical tasks
- Build the history API with pagination
- Add filters for date range and transaction type

### Deliverables
- Transaction history API
- History screen in the customer portal

### Acceptance criteria
- Customers can page through their full history
- Filters return only matching transactions
"""

OLDER_DRAFT = """# Customer Rewards Platform - Implementation Plan (draft)

Synthetic early draft. Superseded by the five-phase plan.

## Phase 1: Customer registration
### Objectives
- Let customers create a rewards account

## Phase 2: Reward balance lookup
### Objectives
- Show the points balance

## Phase 3: Points redemption
### Objectives
- Redeem points for rewards

## Phase 4: Transaction history
### Objectives
- Show past transactions
"""

SOURCE_NAME = "Customer Rewards Platform implementation plan.md"


@dataclass(frozen=True)
class SourceDocument:
    file_id: str
    name: str
    content: str
    modified_time: str
    mime_type: str = SOURCE_MIME

    @property
    def sha256(self) -> str:
        return sha256_text(self.content)

    @property
    def size_bytes(self) -> int:
        return len(self.content.encode("utf-8"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SOURCE_FIXTURES: dict[str, SourceDocument] = {
    doc.file_id: doc
    for doc in (
        SourceDocument("mock-src-crp-plan-v2", SOURCE_NAME, CUSTOMER_REWARDS_PLAN, "2026-11-06T09:30:00Z"),
        SourceDocument("mock-src-crp-plan-v1", SOURCE_NAME, OLDER_DRAFT, "2026-10-20T14:05:00Z"),
    )
}
