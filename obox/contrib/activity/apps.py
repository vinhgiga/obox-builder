"""Optional activity application."""

from obox.contrib._apps import FeatureConfig


class ActivityConfig(FeatureConfig):
    name = "obox.contrib.activity"
    feature = "Activity"
    setting = "ACTIVITY"
