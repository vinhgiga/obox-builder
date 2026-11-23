"""Optional webhooks application."""

from obox.contrib._apps import FeatureConfig


class WebhooksConfig(FeatureConfig):
    name = "obox.contrib.webhooks"
    feature = "Webhooks"
    setting = "WEBHOOKS"
