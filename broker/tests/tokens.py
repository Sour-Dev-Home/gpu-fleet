"""Bearer tokens for tests: fixed test values, not secrets. Long enough to pass the
broker's MIN_TOKEN_LENGTH, and different from each other (ADR 0011)."""

WORKER = "test-worker-token-0123456789abcdefghij"
ADMIN = "test-admin-token-0123456789abcdefghijk"
