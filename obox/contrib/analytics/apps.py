"""Optional analytics application."""

from obox.contrib._apps import FeatureConfig


class AnalyticsConfig(FeatureConfig):
    name = "obox.contrib.analytics"
    feature = "Analytics"
    setting = "ANALYTICS"
