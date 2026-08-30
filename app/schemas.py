"""Validated request and response contracts for the FleetWatch HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

SQLITE_INTEGER_MAX = 2**63 - 1


def _not_dot_segment(value: str) -> str:
    if value in {".", ".."}:
        raise ValueError("identifier must not be a dot segment")
    return value


Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
    AfterValidator(_not_dot_segment),
]
RobotIdentifier = Annotated[Identifier, Field(max_length=100)]


class StrictModel(BaseModel):
    """Base class used by public inputs so accidental fields are rejected."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PoseCovariance(StrictModel):
    """Planar covariance matrix in square metres."""

    xx: float = Field(ge=0, allow_inf_nan=False, strict=True)
    xy: float = Field(allow_inf_nan=False, strict=True)
    yy: float = Field(ge=0, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def must_be_positive_semidefinite(self) -> PoseCovariance:
        determinant = self.xx * self.yy - self.xy * self.xy
        tolerance = max(1.0, self.xx * self.yy) * 1e-12
        if determinant < -tolerance:
            raise ValueError("pose covariance must be positive semidefinite")
        return self


class Pose(StrictModel):
    frame_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ]
    x_m: float = Field(allow_inf_nan=False, strict=True)
    y_m: float = Field(allow_inf_nan=False, strict=True)
    heading_deg: float = Field(
        allow_inf_nan=False,
        strict=True,
        validation_alias=AliasChoices("heading_deg", "heading_degrees"),
    )
    speed_mps: float = Field(ge=0, allow_inf_nan=False, strict=True)
    covariance: PoseCovariance

    @field_validator("heading_deg")
    @classmethod
    def normalize_heading(cls, value: float) -> float:
        return value % 360.0


class TelemetryEvent(StrictModel):
    schema_version: Literal[1, 2] = 2
    event_id: Annotated[Identifier, Field(max_length=200)]
    robot_id: RobotIdentifier
    timestamp: datetime
    boot_id: Annotated[Identifier, Field(max_length=200)] | None = None
    sequence_number: int | None = Field(
        default=None,
        ge=0,
        le=SQLITE_INTEGER_MAX,
        strict=True,
    )
    mission_id: Annotated[Identifier, Field(max_length=200)] | None = None
    operating_mode: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ]
    battery_percentage: float = Field(
        ge=0,
        le=100,
        allow_inf_nan=False,
        strict=True,
    )
    temperature: float = Field(allow_inf_nan=False, strict=True)
    link_quality_percentage: float | None = Field(
        default=None,
        ge=0,
        le=100,
        allow_inf_nan=False,
        strict=True,
    )
    pose: Pose | None = None

    @field_validator("timestamp", mode="before")
    @classmethod
    def timestamp_must_be_an_iso_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("timestamp must be a valid ISO-8601 string")
        return value.strip()

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset")
        return value

    @model_validator(mode="after")
    def source_sequence_is_paired(self) -> TelemetryEvent:
        if (self.boot_id is None) != (self.sequence_number is None):
            raise ValueError("boot_id and sequence_number must be provided together")
        return self


class TelemetryBatch(StrictModel):
    events: list[TelemetryEvent] = Field(min_length=1, max_length=100)


class ReplayStart(StrictModel):
    ticks_per_second: float = Field(
        default=2.0,
        gt=0,
        le=100,
        allow_inf_nan=False,
        strict=True,
        validation_alias=AliasChoices("ticks_per_second", "events_per_second", "speed"),
    )


AlertType = Literal[
    "low_battery",
    "overheating",
    "stale_telemetry",
    "localization_degraded",
    "geofence_breach",
]
AlertStatus = Literal["opened", "acknowledged", "recovered"]
AlertSeverity = Literal["warning", "critical"]
RobotStatus = Literal["healthy", "warning", "critical"]
LocalizationState = Literal["nominal", "degraded", "unavailable"]
ReplayState = Literal["paused", "running", "complete", "error"]
ClockMode = Literal["virtual", "wall"]


class PoseResponse(BaseModel):
    frame_id: str
    x_m: float
    y_m: float
    heading_deg: float
    speed_mps: float
    covariance: PoseCovariance


class TelemetryResponse(BaseModel):
    event_id: str
    robot_id: str
    timestamp: str
    received_at: str | None = None
    mission_id: str | None = None
    operating_mode: str
    battery_percentage: float
    temperature: float
    link_quality_percentage: float | None = None
    pose: PoseResponse | None = None


class AlertResponse(BaseModel):
    id: int
    robot_id: str
    type: AlertType
    severity: AlertSeverity = "warning"
    status: AlertStatus
    version: int = 1
    opened_at: str
    acknowledged_at: str | None
    recovered_at: str | None
    updated_at: str
    value: float | None
    message: str


class RobotResponse(TelemetryResponse):
    status: RobotStatus
    stale: bool
    active_alerts: list[AlertResponse]
    history_revision: int
    zone_id: str | None = None
    route_id: str | None = None
    localization_state: LocalizationState = "unavailable"
    accuracy_95_m: float | None = Field(
        default=None,
        description=(
            "Covariance-derived nominal 95% planar uncertainty radius in metres; "
            "this is not measured positioning error."
        ),
    )


class FleetSummaryResponse(BaseModel):
    total: int
    healthy: int
    warning: int
    critical: int
    stale: int
    active_alerts: int
    ready: int = 0
    located: int = 0
    moving: int = 0
    localization_degraded: int = 0
    active_missions: int = 0


class HistoryResponse(BaseModel):
    robot_id: str
    events: list[TelemetryResponse]
    count: int
    history_revision: int


class IngestResponse(BaseModel):
    accepted: int
    duplicates: int
    event_ids: list[str]


class ReplayResponse(BaseModel):
    state: ReplayState
    status: ReplayState
    current: int
    total: int
    progress: float
    events_per_second: float
    ticks_per_second: float
    current_tick: int
    total_ticks: int
    events_processed: int
    total_events: int
    last_error: str | None
    revision: int


class DashboardResponse(BaseModel):
    revision: str
    fleet_time: str | None
    clock_mode: ClockMode
    summary: FleetSummaryResponse
    robots: list[RobotResponse]
    alerts: list[AlertResponse]
    replay: ReplayResponse


class SitePoint(StrictModel):
    x_m: float = Field(allow_inf_nan=False, strict=True)
    y_m: float = Field(allow_inf_nan=False, strict=True)


class SiteBounds(StrictModel):
    width_m: float = Field(gt=0, allow_inf_nan=False, strict=True)
    height_m: float = Field(gt=0, allow_inf_nan=False, strict=True)


class SiteZone(StrictModel):
    id: Identifier
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    kind: Literal[
        "floor",
        "aisle",
        "staging",
        "dispatch",
        "charging",
        "inspection",
        "restricted",
    ]
    polygon: list[SitePoint] = Field(min_length=3)


class SiteAnchor(StrictModel):
    id: Identifier
    x_m: float = Field(allow_inf_nan=False, strict=True)
    y_m: float = Field(allow_inf_nan=False, strict=True)


class SiteRoute(StrictModel):
    id: Identifier
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    points: list[SitePoint] = Field(min_length=2)


class SiteRobot(StrictModel):
    id: RobotIdentifier
    display_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    route_id: Identifier


class MetricPolicy(StrictModel):
    open: float = Field(allow_inf_nan=False, strict=True)
    recover: float = Field(allow_inf_nan=False, strict=True)


class BatteryPolicy(StrictModel):
    open: float = Field(ge=0, le=100, allow_inf_nan=False, strict=True)
    recover: float = Field(ge=0, le=100, allow_inf_nan=False, strict=True)


class StalePolicy(StrictModel):
    open_seconds: float = Field(gt=0, allow_inf_nan=False, strict=True)
    recover_seconds: float = Field(ge=0, allow_inf_nan=False, strict=True)


class LocalizationPolicy(StrictModel):
    open_m: float = Field(gt=0, allow_inf_nan=False, strict=True)
    recover_m: float = Field(ge=0, allow_inf_nan=False, strict=True)


class SitePolicies(StrictModel):
    low_battery: BatteryPolicy
    overheating: MetricPolicy
    stale_telemetry: StalePolicy
    localization_degraded: LocalizationPolicy

    @model_validator(mode="after")
    def thresholds_have_hysteresis(self) -> SitePolicies:
        if self.low_battery.open >= self.low_battery.recover:
            raise ValueError("low-battery recovery must exceed its open threshold")
        if self.overheating.open <= self.overheating.recover:
            raise ValueError("overheating recovery must be below its open threshold")
        if self.localization_degraded.open_m <= self.localization_degraded.recover_m:
            raise ValueError("localization recovery must be below its open threshold")
        if self.stale_telemetry.open_seconds < self.stale_telemetry.recover_seconds:
            raise ValueError("stale recovery cannot exceed its open threshold")
        return self


class SiteScenario(StrictModel):
    id: Identifier
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    initial_tick: int = Field(ge=0, strict=True)
    default_ticks_per_second: float = Field(gt=0, le=100, allow_inf_nan=False, strict=True)
    total_ticks: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def initial_tick_exists(self) -> SiteScenario:
        if self.initial_tick >= self.total_ticks:
            raise ValueError("scenario initial_tick must be inside total_ticks")
        return self


class SiteDeployment(StrictModel):
    read_only: bool = False


class SiteResponse(StrictModel):
    site_id: str
    name: str
    frame_id: str
    shift_controls_enabled: Literal[True]
    bounds: SiteBounds
    zones: list[SiteZone]
    anchors: list[SiteAnchor]
    routes: list[SiteRoute]
    robots: list[SiteRobot]
    policies: SitePolicies
    scenario: SiteScenario
    deployment: SiteDeployment = Field(default_factory=SiteDeployment)

    @model_validator(mode="after")
    def validate_references_and_geometry(self) -> SiteResponse:
        for label, identifiers in (
            ("zone", [zone.id for zone in self.zones]),
            ("anchor", [anchor.id for anchor in self.anchors]),
            ("route", [route.id for route in self.routes]),
            ("robot", [robot.id for robot in self.robots]),
        ):
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"site contains duplicate {label} IDs")
        route_ids = {route.id for route in self.routes}
        for robot in self.robots:
            if robot.route_id not in route_ids:
                raise ValueError(f"robot {robot.id!r} references unknown route {robot.route_id!r}")
        points = (
            [point for zone in self.zones for point in zone.polygon]
            + [point for route in self.routes for point in route.points]
            + [SitePoint(x_m=anchor.x_m, y_m=anchor.y_m) for anchor in self.anchors]
        )
        if any(
            point.x_m < 0
            or point.y_m < 0
            or point.x_m > self.bounds.width_m
            or point.y_m > self.bounds.height_m
            for point in points
        ):
            raise ValueError("site geometry must remain inside bounds")
        return self


class HealthResponse(BaseModel):
    status: Literal["ok"]
