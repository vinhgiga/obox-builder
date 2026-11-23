"""Optional idempotency application."""

from obox.contrib._apps import FeatureConfig


class IdempotencyConfig(FeatureConfig):
    name = "obox.contrib.idempotency"
    feature = "Idempotency"
    setting = "IDEMPOTENCY"
