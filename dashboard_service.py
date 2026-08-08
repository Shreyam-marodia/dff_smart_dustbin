"""
Dashboard processing layer.

Sits between the raw `load_cell_logs` table and the frontend. Nothing in
here writes to the database — it's a read-side transform: aggregate stats,
drop bad GPS fixes, smooth noisy weight readings, and downsample the chart
series so the payload stays small as the log table grows.
"""

from dataclasses import dataclass, field
from typing import List, Optional
from statistics import mean


# --- Tunables -----------------------------------------------------------

# Points beyond ~250-300 don't add visible resolution to a line chart on a
# normal screen, and each one is a label + two numbers over the wire.
DEFAULT_MAX_CHART_POINTS = 30

# Simple centered moving average window for smoothing the weight line.
# Odd number so the window is symmetric around each point.
DEFAULT_MOVING_AVG_WINDOW = 5

# A (0, 0) or out-of-range fix means the GPS module hadn't locked yet.
# Rendering it would put a marker in the Gulf of Guinea and mess up
# fitBounds() on the frontend.
def _is_valid_coord(lat: float, lng: float) -> bool:
    if lat == 0 and lng == 0:
        return False
    if not (-90 <= lat <= 90):
        return False
    if not (-180 <= lng <= 180):
        return False
    return True


# --- Output shape ---------------------------------------------------------

@dataclass
class WeightStats:
    count: int
    latest: Optional[float]
    average: Optional[float]
    minimum: Optional[float]
    maximum: Optional[float]


@dataclass
class ChartSeries:
    labels: List[str] = field(default_factory=list)
    weights: List[float] = field(default_factory=list)
    moving_average: List[float] = field(default_factory=list)
    downsampled: bool = False
    raw_point_count: int = 0


@dataclass
class MapPoint:
    latitude: float
    longitude: float
    weight: float
    timestamp: str


@dataclass
class DashboardPayload:
    stats: WeightStats
    chart: ChartSeries
    map_points: List[MapPoint]
    dropped_gps_points: int


class DashboardService:
    """Transforms raw LogEntry rows into a frontend-ready payload."""

    def __init__(
        self,
        max_chart_points: int = DEFAULT_MAX_CHART_POINTS,
        moving_avg_window: int = DEFAULT_MOVING_AVG_WINDOW,
    ):
        self.max_chart_points = max_chart_points
        self.moving_avg_window = moving_avg_window

    def build(self, logs: list, db_stats: tuple = None) -> DashboardPayload:
        """
        `logs` is a list of ORM rows (or anything with .weight, .latitude,
        .longitude, .timestamp attributes), already ordered oldest-first.
        """
        if not logs:
            return DashboardPayload(
                stats=WeightStats(count=0, latest=None, average=None, minimum=None, maximum=None),
                chart=ChartSeries(),
                map_points=[],
                dropped_gps_points=0,
            )

        # Use database stats if provided, otherwise fallback to Python math
        if db_stats:
            total_count, avg_weight, min_weight, max_weight = db_stats
            stats = WeightStats(
                count=total_count or 0,
                latest=logs[-1].weight,
                average=round(avg_weight, 3) if avg_weight is not None else None,
                minimum=min_weight,
                maximum=max_weight,
            )
        else:
            weights = [log.weight for log in logs]
            stats = WeightStats(
                count=len(logs),
                latest=weights[-1],
                average=round(mean(weights), 3),
                minimum=min(weights),
                maximum=max(weights),
            )

        chart = self._build_chart_series(logs)

        map_points = []
        dropped_gps_points = 0
        for log in logs:
            if _is_valid_coord(log.latitude, log.longitude):
                map_points.append(
                    MapPoint(
                        latitude=log.latitude,
                        longitude=log.longitude,
                        weight=log.weight,
                        timestamp=log.timestamp,
                    )
                )
            else:
                dropped_gps_points += 1

        return DashboardPayload(
            stats=stats,
            chart=chart,
            map_points=map_points,
            dropped_gps_points=dropped_gps_points,
        )

    def _build_chart_series(self, logs: list) -> ChartSeries:
        raw_count = len(logs)
        sampled = self._downsample(logs, self.max_chart_points)

        weights = [log.weight for log in sampled]
        labels = [log.timestamp for log in sampled]
        smoothed = self._moving_average(weights, self.moving_avg_window)

        return ChartSeries(
            labels=labels,
            weights=weights,
            moving_average=smoothed,
            downsampled=(len(sampled) < raw_count),
            raw_point_count=raw_count,
        )

    @staticmethod
    def _downsample(logs: list, max_points: int) -> list:
        """
        Evenly-spaced sampling (not just truncating to the last N) so the
        chart still shows the full time range instead of only recent data.
        Always keeps the first and last point.
        """
        n = len(logs)
        if n <= max_points:
            return logs

        step = n / max_points
        indices = sorted({min(n - 1, int(i * step)) for i in range(max_points)})
        indices[-1] = n - 1
        return [logs[i] for i in indices]

    @staticmethod
    def _moving_average(values: List[float], window: int) -> List[float]:
        if window < 2 or len(values) < window:
            return list(values)

        half = window // 2
        result = []
        for i in range(len(values)):
            lo = max(0, i - half)
            hi = min(len(values), i + half + 1)
            result.append(round(mean(values[lo:hi]), 3))
        return result