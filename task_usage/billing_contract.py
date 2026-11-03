"""Pinned membership migration shared by backend and workers."""
BILLING_VERSION = "0025_membership_subscriptions"
BILLING_CHECKSUM = "ced7418143e8b1cb22e45f72ff0d5e53190b348a85fb7951f96a5cc7a5ff3db5"
BILLING_TABLES = ("billing.accounts", "billing.subscriptions", "billing.orders", "billing.payment_events")
