"""Optional analytics application."""

from __future__ import annotations

from typing import TYPE_CHECKING

from obox.contrib._apps import FeatureConfig
from obox.core.config import require_positive_integers

if TYPE_CHECKING:
    from obox.runtime.runtime import _Runtime


class AnalyticsConfig(FeatureConfig):
    name = "obox.contrib.analytics"
    feature = "Analytics"
    setting = "ANALYTICS"


class Analytics:
    name = "analytics"
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        max_rows: int = 200,
        max_columns: int = 50,
        max_cells: int = 10_000,
        max_chart_groups: int = 200,
        max_pie_groups: int = 50,
        max_points: int = 10_000,
        timeout_ms: int = 3000,
    ) -> None:
        limits = (
            max_rows,
            max_columns,
            max_cells,
            max_chart_groups,
            max_pie_groups,
            max_points,
            timeout_ms,
        )
        require_positive_integers(limits, "Analytics limits must be positive integers.")
        self.max_rows = max_rows
        self.max_columns = max_columns
        self.max_cells = max_cells
        self.max_chart_groups = max_chart_groups
        self.max_pie_groups = max_pie_groups
        self.max_points = max_points
        self.timeout_ms = timeout_ms

    def _attach(self, obox: _Runtime) -> None:
        pass
