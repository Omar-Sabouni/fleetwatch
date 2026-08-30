"""Validated site metadata and lightweight planar geometry helpers."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .schemas import SiteResponse


class SiteConfigurationError(ValueError):
    """Packaged site metadata is missing or violates the public contract."""


class SiteConfiguration:
    """Immutable, validated view of the facility and scenario configuration."""

    def __init__(self, payload: dict[str, Any], source: Path | None = None):
        try:
            self._model = SiteResponse.model_validate(payload)
        except ValueError as exc:
            location = f" at {source}" if source is not None else ""
            raise SiteConfigurationError(f"invalid site configuration{location}: {exc}") from exc
        self.source = source
        self._payload = self._model.model_dump(mode="json")
        self._routes_by_robot = {
            str(robot["id"]): str(robot["route_id"])
            for robot in self._payload["robots"]
            if robot.get("id") is not None and robot.get("route_id") is not None
        }

    @classmethod
    def load(cls, path: str | Path) -> SiteConfiguration:
        source = Path(path)
        if not source.is_file():
            raise SiteConfigurationError(f"site configuration is missing: {source}")
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SiteConfigurationError(f"could not read site configuration: {source}") from exc
        if not isinstance(raw, dict):
            raise SiteConfigurationError("site configuration must be a JSON object")
        return cls(raw, source)

    def public_payload(self) -> dict[str, Any]:
        return copy.deepcopy(self._payload)

    @property
    def frame_id(self) -> str:
        return str(self._payload["frame_id"])

    @property
    def shift_controls_enabled(self) -> bool:
        return bool(self._payload["shift_controls_enabled"])

    def route_for_robot(self, robot_id: str) -> str | None:
        return self._routes_by_robot.get(robot_id)

    def policy(self, name: str) -> dict[str, Any]:
        raw = self._payload["policies"].get(name, {})
        return dict(raw) if isinstance(raw, dict) else {}

    def restricted_zone_at(self, x_m: float, y_m: float) -> str | None:
        for zone in self._payload["zones"]:
            if zone.get("kind") != "restricted":
                continue
            polygon = zone.get("polygon")
            if isinstance(polygon, list) and point_in_polygon(x_m, y_m, polygon):
                return str(zone["id"])
        return None

    def zone_at(self, x_m: float, y_m: float) -> str | None:
        # Prefer operational regions over the floor/aisle backdrop. Restricted
        # regions remain visible as the current zone as well as an incident.
        zones = sorted(
            self._payload["zones"],
            key=lambda zone: zone.get("kind") in {"floor", "aisle"},
        )
        for zone in zones:
            polygon = zone.get("polygon")
            if isinstance(polygon, list) and point_in_polygon(x_m, y_m, polygon):
                return str(zone["id"])
        return None


def _point_on_segment(
    x_m: float,
    y_m: float,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
) -> bool:
    cross = (x_m - start_x) * (end_y - start_y) - (y_m - start_y) * (end_x - start_x)
    tolerance = 1e-9 * max(
        1.0,
        abs(x_m),
        abs(y_m),
        abs(start_x),
        abs(start_y),
        abs(end_x),
        abs(end_y),
    )
    if abs(cross) > tolerance:
        return False
    dot = (x_m - start_x) * (x_m - end_x) + (y_m - start_y) * (y_m - end_y)
    return dot <= tolerance


def point_in_polygon(x_m: float, y_m: float, polygon: list[dict[str, Any]]) -> bool:
    """Return boundary-inclusive containment for a simple planar polygon."""

    if len(polygon) < 3:
        return False
    points: list[tuple[float, float]] = []
    try:
        points = [(float(point["x_m"]), float(point["y_m"])) for point in polygon]
    except (KeyError, TypeError, ValueError):
        return False

    inside = False
    previous_x, previous_y = points[-1]
    for current_x, current_y in points:
        if _point_on_segment(
            x_m,
            y_m,
            previous_x,
            previous_y,
            current_x,
            current_y,
        ):
            return True
        crosses = (current_y > y_m) != (previous_y > y_m)
        if crosses:
            intersection_x = (previous_x - current_x) * (y_m - current_y) / (
                previous_y - current_y
            ) + current_x
            if x_m < intersection_x:
                inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside
