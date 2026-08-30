"""SQLite persistence, projections, and fleet-health domain logic.

FleetWatch intentionally uses one SQLite connection guarded by a re-entrant
lock. That makes an equal-time fleet tick atomic, keeps in-memory databases
usable in tests, and gives dashboard readers a coherent projection snapshot.
"""

from __future__ import annotations

import itertools
import json
import math
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .schemas import TelemetryEvent
from .site import SiteConfiguration

LOW_BATTERY_OPEN = 20.0
LOW_BATTERY_RECOVER = 25.0
OVERHEATING_OPEN = 75.0
OVERHEATING_RECOVER = 70.0
STALE_AFTER_SECONDS = 120.0
LOCALIZATION_OPEN_METRES = 3.0
LOCALIZATION_RECOVER_METRES = 2.0
MAX_FUTURE_SKEW_SECONDS = 300.0
MAX_BATCH_EVENTS = 100
SCHEMA_VERSION = 2

ACTIVE_STATUSES = ("opened", "acknowledged")
ALERT_TYPES = (
    "low_battery",
    "overheating",
    "stale_telemetry",
    "localization_degraded",
    "geofence_breach",
)
CRITICAL_ALERT_TYPES = ("overheating", "geofence_breach")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")


class EventIDConflict(ValueError):
    """An event ID was reused for a different canonical payload."""

    def __init__(self, event_id: str):
        self.event_id = event_id
        super().__init__(f"event_id {event_id!r} is already used by different telemetry")


class SourceSequenceConflict(ValueError):
    """A robot boot sequence number was reused by another event."""

    def __init__(self, robot_id: str, boot_id: str, sequence_number: int):
        self.robot_id = robot_id
        self.boot_id = boot_id
        self.sequence_number = sequence_number
        super().__init__(
            "source sequence is already assigned to different telemetry "
            f"({robot_id!r}, {boot_id!r}, {sequence_number})"
        )


class FutureTimestampError(ValueError):
    """Untrusted device telemetry is too far ahead of trusted receipt time."""

    def __init__(self, event_id: str, skew_seconds: float):
        self.event_id = event_id
        self.skew_seconds = skew_seconds
        super().__init__(
            f"event {event_id!r} is {skew_seconds:g} seconds ahead of the trusted clock"
        )


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> tuple[str, float]:
    """Return a canonical UTC ISO-8601 timestamp and its Unix epoch value."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            raise ValueError("timestamp must not be empty")
        if candidate.endswith(("Z", "z")):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError("timestamp must be a valid ISO-8601 value") from exc
    else:
        raise ValueError("timestamp must be a valid ISO-8601 string")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    parsed = parsed.astimezone(UTC)
    try:
        epoch = parsed.timestamp()
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("timestamp is outside the supported range") from exc
    canonical = parsed.isoformat().replace("+00:00", "Z")
    return canonical, epoch


def _as_mapping(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return event
    if hasattr(event, "model_dump"):
        value = event.model_dump()
        if isinstance(value, dict):
            return value
    if hasattr(event, "dict"):
        value = event.dict()
        if isinstance(value, dict):
            return value
    raise ValueError("telemetry event must be a JSON object")


def _accuracy_95_m(xx: float, xy: float, yy: float) -> float:
    eigenvalue = (xx + yy + math.hypot(xx - yy, 2.0 * xy)) / 2.0
    accuracy = math.sqrt(max(0.0, 5.991 * eigenvalue))
    if not math.isfinite(accuracy):
        raise ValueError("pose covariance produces a non-finite localization accuracy")
    return accuracy


def normalize_event(event: Any) -> dict[str, Any]:
    """Validate and canonicalize one public telemetry object."""

    if isinstance(event, TelemetryEvent):
        model = event
    else:
        try:
            model = TelemetryEvent.model_validate(_as_mapping(event))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    raw = model.model_dump(mode="python")
    timestamp, timestamp_epoch = parse_timestamp(raw["timestamp"])
    pose = raw.get("pose")
    accuracy_95_m: float | None = None
    if pose is not None:
        covariance = pose["covariance"]
        accuracy_95_m = _accuracy_95_m(
            float(covariance["xx"]),
            float(covariance["xy"]),
            float(covariance["yy"]),
        )
    return {
        "schema_version": int(raw["schema_version"]),
        "event_id": str(raw["event_id"]),
        "robot_id": str(raw["robot_id"]),
        "timestamp": timestamp,
        "timestamp_epoch": timestamp_epoch,
        "boot_id": raw.get("boot_id"),
        "sequence_number": raw.get("sequence_number"),
        "mission_id": raw.get("mission_id"),
        "operating_mode": str(raw["operating_mode"]),
        "battery_percentage": float(raw["battery_percentage"]),
        "temperature": float(raw["temperature"]),
        "link_quality_percentage": (
            None
            if raw.get("link_quality_percentage") is None
            else float(raw["link_quality_percentage"])
        ),
        "pose": pose,
        "accuracy_95_m": accuracy_95_m,
    }


def load_events_jsonl(telemetry_path: str | Path) -> list[dict[str, Any]]:
    """Load a JSONL fixture and retain only canonical public event fields."""

    path = Path(telemetry_path)
    events: list[dict[str, Any]] = []
    try:
        source = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"telemetry fixture is unavailable: {path}") from exc
    with source:
        for line_number, line in enumerate(source, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
                if not isinstance(value, dict):
                    raise ValueError("event must be an object")
                canonical = normalize_event(value)
                canonical.pop("timestamp_epoch")
                canonical.pop("accuracy_95_m")
                events.append(canonical)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"invalid telemetry JSON on line {line_number}: {exc}") from exc
    if not events:
        raise ValueError(f"telemetry fixture contains no events: {path}")
    event_ids = [str(event["event_id"]) for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("telemetry fixture contains duplicate event IDs")
    return events


class FleetStore:
    """Persistence facade containing FleetWatch transition rules."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        site: SiteConfiguration | None = None,
        clock_mode: Literal["virtual", "wall"] = "virtual",
        now_provider: Callable[[], str | datetime] | None = None,
    ):
        self.db_path = str(db_path)
        self.site = site
        self.clock_mode = clock_mode
        low_battery = site.policy("low_battery") if site is not None else {}
        overheating = site.policy("overheating") if site is not None else {}
        stale = site.policy("stale_telemetry") if site is not None else {}
        localization = site.policy("localization_degraded") if site is not None else {}
        self.low_battery_open = float(low_battery.get("open", LOW_BATTERY_OPEN))
        self.low_battery_recover = float(low_battery.get("recover", LOW_BATTERY_RECOVER))
        self.overheating_open = float(overheating.get("open", OVERHEATING_OPEN))
        self.overheating_recover = float(overheating.get("recover", OVERHEATING_RECOVER))
        self.stale_after_seconds = float(stale.get("open_seconds", STALE_AFTER_SECONDS))
        self.stale_recover_seconds = float(stale.get("recover_seconds", STALE_AFTER_SECONDS))
        self.localization_open_metres = float(localization.get("open_m", LOCALIZATION_OPEN_METRES))
        self.localization_recover_metres = float(
            localization.get("recover_m", LOCALIZATION_RECOVER_METRES)
        )
        self._now_provider = now_provider or utc_now
        self._lock = threading.RLock()
        self._closed = False
        self._revision = 0
        self.connection = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.initialize_schema()

    def _now(self) -> tuple[str, float]:
        return parse_timestamp(self._now_provider())

    def initialize_schema(self) -> None:
        """Apply the raw-SQL v2 migration atomically and idempotently."""

        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                previous_schema_version = int(
                    self.connection.execute("PRAGMA user_version").fetchone()[0]
                )
                tables = {
                    str(row["name"])
                    for row in self.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                migrated_legacy_schema = False
                if "telemetry" in tables:
                    migrated_legacy_schema = self._migrate_telemetry_if_needed()
                else:
                    self._create_telemetry_table()
                self._create_robot_revision_table()
                if "alerts" in tables:
                    migrated_legacy_schema = (
                        self._migrate_alerts_if_needed() or migrated_legacy_schema
                    )
                else:
                    self._create_alerts_table()
                self._create_projection_and_transition_tables()
                self._create_metadata_table()
                self._record_schema_provenance(
                    previous_schema_version,
                    migrated_legacy_schema=migrated_legacy_schema,
                )
                self._create_indexes()
                self._backfill_robot_revisions()
                self._backfill_robot_state()
                self._backfill_virtual_clock()
                self._backfill_alert_events()
                self._normalize_alert_transition_versions()
                self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def _create_telemetry_table(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE telemetry (
                event_id TEXT PRIMARY KEY,
                robot_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 2,
                timestamp TEXT NOT NULL,
                timestamp_epoch REAL NOT NULL,
                boot_id TEXT,
                sequence_number INTEGER CHECK (
                    sequence_number IS NULL OR sequence_number >= 0
                ),
                mission_id TEXT,
                operating_mode TEXT NOT NULL,
                battery_percentage REAL NOT NULL,
                temperature REAL NOT NULL,
                link_quality_percentage REAL,
                pose_frame_id TEXT,
                pose_x_m REAL,
                pose_y_m REAL,
                pose_heading_deg REAL,
                pose_speed_mps REAL,
                covariance_xx REAL,
                covariance_xy REAL,
                covariance_yy REAL,
                accuracy_95_m REAL,
                received_at TEXT NOT NULL,
                received_epoch REAL NOT NULL,
                projection_applied INTEGER NOT NULL DEFAULT 1 CHECK (
                    projection_applied IN (0, 1)
                ),
                CHECK ((boot_id IS NULL) = (sequence_number IS NULL))
            )
            """
        )

    def _migrate_telemetry_if_needed(self) -> bool:
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(telemetry)").fetchall()
        }
        if {
            "schema_version",
            "received_at",
            "received_epoch",
            "projection_applied",
            "pose_x_m",
        }.issubset(columns):
            return False
        self.connection.execute("ALTER TABLE telemetry RENAME TO telemetry_legacy_v1")
        self._create_telemetry_table()
        self.connection.execute(
            """
            INSERT INTO telemetry (
                event_id, robot_id, schema_version, timestamp, timestamp_epoch,
                operating_mode, battery_percentage, temperature, received_at,
                received_epoch, projection_applied
            )
            SELECT event_id, robot_id, 1, timestamp, timestamp_epoch,
                   operating_mode, battery_percentage, temperature, ingested_at,
                   (julianday(ingested_at) - 2440587.5) * 86400.0, 1
            FROM telemetry_legacy_v1
            ORDER BY rowid
            """
        )
        self.connection.execute("DROP TABLE telemetry_legacy_v1")
        return True

    def _create_robot_revision_table(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS robot_revisions (
                robot_id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL CHECK (revision >= 0)
            )
            """
        )

    def _create_alerts_table(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                robot_id TEXT NOT NULL,
                alert_type TEXT NOT NULL CHECK (
                    alert_type IN (
                        'low_battery', 'overheating', 'stale_telemetry',
                        'localization_degraded', 'geofence_breach'
                    )
                ),
                severity TEXT NOT NULL CHECK (severity IN ('warning', 'critical')),
                status TEXT NOT NULL CHECK (
                    status IN ('opened', 'acknowledged', 'recovered')
                ),
                version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
                opened_at TEXT NOT NULL,
                opened_epoch REAL NOT NULL,
                acknowledged_at TEXT,
                recovered_at TEXT,
                updated_at TEXT NOT NULL,
                last_event_id TEXT,
                value REAL,
                message TEXT NOT NULL
            )
            """
        )

    def _migrate_alerts_if_needed(self) -> bool:
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(alerts)").fetchall()
        }
        definition_row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'alerts'"
        ).fetchone()
        definition = str(definition_row["sql"] or "") if definition_row else ""
        if {"severity", "version"}.issubset(columns) and "geofence_breach" in definition:
            return False
        self.connection.execute("ALTER TABLE alerts RENAME TO alerts_legacy_v1")
        self._create_alerts_table()
        self.connection.execute(
            """
            INSERT INTO alerts (
                id, robot_id, alert_type, severity, status, version,
                opened_at, opened_epoch, acknowledged_at, recovered_at,
                updated_at, last_event_id, value, message
            )
            SELECT id, robot_id, alert_type,
                   CASE WHEN alert_type = 'overheating' THEN 'critical'
                        ELSE 'warning' END,
                   status,
                   CASE
                       WHEN acknowledged_at IS NOT NULL AND recovered_at IS NOT NULL THEN 3
                       WHEN acknowledged_at IS NOT NULL OR recovered_at IS NOT NULL THEN 2
                       ELSE 1
                   END,
                   opened_at, opened_epoch, acknowledged_at,
                   recovered_at, updated_at, last_event_id, value, message
            FROM alerts_legacy_v1
            ORDER BY id
            """
        )
        self.connection.execute("DROP TABLE alerts_legacy_v1")
        return True

    def _create_projection_and_transition_tables(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS robot_state (
                robot_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE REFERENCES telemetry(event_id)
                    ON DELETE CASCADE
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
                transition_type TEXT NOT NULL CHECK (
                    transition_type IN ('opened', 'acknowledged', 'recovered')
                ),
                occurred_at TEXT NOT NULL,
                occurred_epoch REAL NOT NULL,
                actor TEXT NOT NULL,
                alert_version INTEGER NOT NULL CHECK (alert_version >= 1),
                source_event_id TEXT,
                value REAL,
                message TEXT NOT NULL
            )
            """
        )

    def _create_metadata_table(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS application_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

    def _record_schema_provenance(
        self,
        previous_schema_version: int,
        *,
        migrated_legacy_schema: bool,
    ) -> None:
        """Keep the database's origin visible without rewriting row provenance."""

        legacy_row = self.connection.execute(
            "SELECT 1 FROM telemetry WHERE schema_version = 1 LIMIT 1"
        ).fetchone()
        provenance = (
            "migrated-v1" if migrated_legacy_schema or legacy_row is not None else "created-v2"
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO application_metadata (key, value)
            VALUES ('schema_provenance', ?)
            """,
            (provenance,),
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO application_metadata (key, value)
            VALUES ('schema_previous_version', ?)
            """,
            (str(previous_schema_version),),
        )

    def _create_indexes(self) -> None:
        statements = (
            "CREATE INDEX IF NOT EXISTS idx_telemetry_robot_time "
            "ON telemetry(robot_id, timestamp_epoch, event_id)",
            "CREATE INDEX IF NOT EXISTS idx_telemetry_time ON telemetry(timestamp_epoch, event_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_telemetry_source_sequence "
            "ON telemetry(robot_id, boot_id, sequence_number) "
            "WHERE boot_id IS NOT NULL AND sequence_number IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_alerts_robot_opened "
            "ON alerts(robot_id, opened_epoch, id)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, alert_type)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_opened ON alerts(opened_epoch DESC, id DESC)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_alert "
            "ON alerts(robot_id, alert_type) "
            "WHERE status IN ('opened', 'acknowledged')",
            "CREATE INDEX IF NOT EXISTS idx_alert_events_alert "
            "ON alert_events(alert_id, occurred_epoch, id)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_robot_recent "
            "ON alerts(robot_id, opened_epoch DESC, id DESC)",
        )
        for statement in statements:
            self.connection.execute(statement)

    def _backfill_robot_revisions(self) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO robot_revisions (robot_id, revision)
            SELECT robot_id, COUNT(*)
            FROM telemetry
            GROUP BY robot_id
            """
        )

    def _backfill_robot_state(self) -> None:
        count = self.connection.execute("SELECT COUNT(*) AS count FROM robot_state").fetchone()
        if count is not None and int(count["count"]) > 0:
            return
        self.connection.execute(
            """
            INSERT OR REPLACE INTO robot_state (robot_id, event_id)
            SELECT robot_id, event_id
            FROM (
                SELECT robot_id, event_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY robot_id
                           ORDER BY timestamp_epoch DESC, event_id DESC
                       ) AS rank
                FROM telemetry
            )
            WHERE rank = 1
            """
        )

    def _backfill_virtual_clock(self) -> None:
        rows = self.connection.execute(
            """
            SELECT timestamp, timestamp_epoch, received_at, received_epoch
            FROM telemetry
            """
        ).fetchall()
        virtual_candidates = [
            (
                (str(row["timestamp"]), float(row["timestamp_epoch"]))
                if float(row["timestamp_epoch"]) <= float(row["received_epoch"])
                else (str(row["received_at"]), float(row["received_epoch"]))
            )
            for row in rows
        ]
        if self.metadata_value("virtual_clock_at") is None and virtual_candidates:
            self._set_virtual_clock(max(virtual_candidates, key=lambda item: item[1]))
        if self.metadata_value("wall_clock_at") is None and rows:
            self._set_wall_clock(
                max(
                    ((str(row["received_at"]), float(row["received_epoch"])) for row in rows),
                    key=lambda item: item[1],
                )
            )

    def _backfill_alert_events(self) -> None:
        count = self.connection.execute("SELECT COUNT(*) AS count FROM alert_events").fetchone()
        if count is not None and int(count["count"]) > 0:
            return
        alerts = self.connection.execute("SELECT * FROM alerts ORDER BY id").fetchall()
        for alert in alerts:
            self._insert_alert_event(
                alert_id=int(alert["id"]),
                transition_type="opened",
                occurred_at=str(alert["opened_at"]),
                occurred_epoch=float(alert["opened_epoch"]),
                actor="migration",
                version=1,
                source_event_id=None,
                value=alert["value"],
                message=str(alert["message"]),
            )
            later_transitions: list[tuple[float, int, str, str]] = []
            for type_order, (transition_type, field) in enumerate(
                (("acknowledged", "acknowledged_at"), ("recovered", "recovered_at"))
            ):
                raw_timestamp = alert[field]
                if raw_timestamp is not None:
                    occurred_at, occurred_epoch = parse_timestamp(raw_timestamp)
                    later_transitions.append(
                        (occurred_epoch, type_order, transition_type, occurred_at)
                    )
            for transition_version, (
                occurred_epoch,
                _,
                transition_type,
                occurred_at,
            ) in enumerate(sorted(later_transitions), start=2):
                self._insert_alert_event(
                    alert_id=int(alert["id"]),
                    transition_type=transition_type,
                    occurred_at=occurred_at,
                    occurred_epoch=occurred_epoch,
                    actor="migration",
                    version=transition_version,
                    source_event_id=None,
                    value=alert["value"],
                    message=str(alert["message"]),
                )

    def _normalize_alert_transition_versions(self) -> None:
        """Make lifecycle versions contiguous and chronological after upgrades."""

        alerts = self.connection.execute("SELECT id FROM alerts ORDER BY id").fetchall()
        for alert in alerts:
            transitions = self.connection.execute(
                """
                SELECT id FROM alert_events
                WHERE alert_id = ?
                ORDER BY occurred_epoch, id
                """,
                (alert["id"],),
            ).fetchall()
            for version, transition in enumerate(transitions, start=1):
                self.connection.execute(
                    "UPDATE alert_events SET alert_version = ? WHERE id = ?",
                    (version, transition["id"]),
                )
            if transitions:
                self.connection.execute(
                    "UPDATE alerts SET version = ? WHERE id = ?",
                    (len(transitions), alert["id"]),
                )

    @property
    def revision(self) -> int:
        return self.current_revision()

    def current_revision(self) -> int:
        with self._lock:
            return self._revision

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.connection.close()
            self._closed = True

    def probe(self) -> None:
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 AS ready WHERE (SELECT user_version FROM pragma_user_version) = ?",
                (SCHEMA_VERSION,),
            ).fetchone()
            if row is None or int(row["ready"]) != 1:
                raise sqlite3.DatabaseError("SQLite schema readiness probe failed")
            if self.connection.in_transaction:
                raise sqlite3.DatabaseError("SQLite readiness probe found an active transaction")
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    """
                    INSERT INTO application_metadata (key, value)
                    VALUES ('readiness_write_probe', hex(randomblob(16)))
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """
                )
            except Exception:
                self._rollback_if_active()
                raise
            else:
                self.connection.execute("ROLLBACK")

    def _rollback_if_active(self) -> None:
        if self.connection.in_transaction:
            self.connection.execute("ROLLBACK")

    def telemetry_count(self) -> int:
        with self._lock:
            row = self.connection.execute("SELECT COUNT(*) AS count FROM telemetry").fetchone()
            return int(row["count"])

    def metadata_value(self, key: str) -> str | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT value FROM application_metadata WHERE key = ?", (key,)
            ).fetchone()
            return None if row is None else str(row["value"])

    def set_metadata_value(self, key: str, value: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO application_metadata (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def replay_control_revision(self) -> int:
        raw = self.metadata_value("replay_control_revision")
        if raw is None:
            return 0
        try:
            revision = int(raw)
        except ValueError as exc:
            raise sqlite3.DatabaseError("invalid persisted replay revision") from exc
        if revision < 0:
            raise sqlite3.DatabaseError("invalid persisted replay revision")
        return revision

    def next_replay_control_revision(self, current_floor: int = 0) -> int:
        """Atomically persist the next operator-control generation."""

        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute(
                    """
                    SELECT value FROM application_metadata
                    WHERE key = 'replay_control_revision'
                    """
                ).fetchone()
                persisted = 0 if row is None else int(row["value"])
                revision = max(persisted, current_floor) + 1
                self.connection.execute(
                    """
                    INSERT INTO application_metadata (key, value)
                    VALUES ('replay_control_revision', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(revision),),
                )
                self.connection.execute("COMMIT")
            except Exception:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
        return revision

    def _virtual_clock(self) -> tuple[str, float] | None:
        row = self.connection.execute(
            "SELECT value FROM application_metadata WHERE key = 'virtual_clock_at'"
        ).fetchone()
        if row is None:
            return None
        return parse_timestamp(row["value"])

    def _set_virtual_clock(self, clock: tuple[str, float]) -> None:
        self.connection.execute(
            """
            INSERT INTO application_metadata (key, value)
            VALUES ('virtual_clock_at', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (clock[0],),
        )

    def _advance_virtual_clock(self, candidate: tuple[str, float]) -> bool:
        current = self._virtual_clock()
        if current is None or candidate[1] > current[1]:
            self._set_virtual_clock(candidate)
            return True
        return False

    def _wall_clock(self) -> tuple[str, float] | None:
        row = self.connection.execute(
            "SELECT value FROM application_metadata WHERE key = 'wall_clock_at'"
        ).fetchone()
        if row is None:
            return None
        return parse_timestamp(row["value"])

    def _set_wall_clock(self, clock: tuple[str, float]) -> None:
        self.connection.execute(
            """
            INSERT INTO application_metadata (key, value)
            VALUES ('wall_clock_at', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (clock[0],),
        )

    def _advance_wall_clock(self, candidate: tuple[str, float]) -> bool:
        current = self._wall_clock()
        if current is None or candidate[1] > current[1]:
            self._set_wall_clock(candidate)
            return True
        return False

    def has_event(self, event_id: str) -> bool:
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM telemetry WHERE event_id = ?", (event_id,)
            ).fetchone()
            return row is not None

    def event_status(self, event: Any) -> str:
        normalized = normalize_event(event)
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM telemetry WHERE event_id = ?",
                (normalized["event_id"],),
            ).fetchone()
            if row is None:
                return "missing"
            return "exact" if self._same_event(row, normalized) else "conflict"

    def seed_from_jsonl(self, telemetry_path: str | Path) -> dict[str, Any]:
        events = load_events_jsonl(telemetry_path)
        events.sort(key=lambda event: parse_timestamp(event["timestamp"])[1])
        accepted = duplicates = 0
        event_ids: list[str] = []
        for _, group in itertools.groupby(
            events, key=lambda event: parse_timestamp(event["timestamp"])[1]
        ):
            tick = list(group)
            if len(tick) > MAX_BATCH_EVENTS:
                raise ValueError(
                    f"one fixture tick exceeds the {MAX_BATCH_EVENTS}-event batch limit"
                )
            partial = self.ingest_events(tick, trusted=True)
            accepted += int(partial["accepted"])
            duplicates += int(partial["duplicates"])
            event_ids.extend(partial["event_ids"])
        return {
            "accepted": accepted,
            "duplicates": duplicates,
            "event_ids": event_ids,
        }

    @staticmethod
    def _same_event(row: sqlite3.Row, event: dict[str, Any]) -> bool:
        pose = event["pose"]
        covariance = pose["covariance"] if pose is not None else None
        legacy_schema_compatible = (
            int(row["schema_version"]) == 1
            and event["schema_version"] == 2
            and event["boot_id"] is None
            and event["sequence_number"] is None
            and event["mission_id"] is None
            and event["link_quality_percentage"] is None
            and pose is None
        )
        comparisons = (
            row["event_id"] == event["event_id"],
            row["robot_id"] == event["robot_id"],
            int(row["schema_version"]) == event["schema_version"] or legacy_schema_compatible,
            row["timestamp"] == event["timestamp"],
            row["boot_id"] == event["boot_id"],
            row["sequence_number"] == event["sequence_number"],
            row["mission_id"] == event["mission_id"],
            row["operating_mode"] == event["operating_mode"],
            float(row["battery_percentage"]) == event["battery_percentage"],
            float(row["temperature"]) == event["temperature"],
            row["link_quality_percentage"] == event["link_quality_percentage"],
            row["pose_frame_id"] == (pose["frame_id"] if pose else None),
            row["pose_x_m"] == (pose["x_m"] if pose else None),
            row["pose_y_m"] == (pose["y_m"] if pose else None),
            row["pose_heading_deg"] == (pose["heading_deg"] if pose else None),
            row["pose_speed_mps"] == (pose["speed_mps"] if pose else None),
            row["covariance_xx"] == (covariance["xx"] if covariance else None),
            row["covariance_xy"] == (covariance["xy"] if covariance else None),
            row["covariance_yy"] == (covariance["yy"] if covariance else None),
        )
        return all(comparisons)

    def ingest_events(
        self,
        events: Iterable[Any],
        *,
        trusted: bool = False,
        received_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        raw_events = list(events)
        if not raw_events:
            raise ValueError("telemetry batch must contain at least one event")
        if len(raw_events) > MAX_BATCH_EVENTS:
            raise ValueError(f"telemetry batch may contain at most {MAX_BATCH_EVENTS} events")
        normalized = [normalize_event(event) for event in raw_events]
        # Robot ID and event ID provide a stable total order for events whose
        # source timestamps are identical. Projection winners use the same
        # event-ID tie break, so request ordering cannot change fleet state.
        normalized.sort(
            key=lambda item: (
                item["timestamp_epoch"],
                item["robot_id"],
                item["event_id"],
            )
        )
        if self.site is not None:
            for event in normalized:
                pose = event["pose"]
                if pose is not None and pose["frame_id"] != self.site.frame_id:
                    raise ValueError(
                        f"pose.frame_id must match the configured site frame {self.site.frame_id!r}"
                    )
        receipt_at, receipt_epoch = (
            parse_timestamp(received_at) if received_at is not None else self._now()
        )
        if not trusted:
            for event in normalized:
                skew = float(event["timestamp_epoch"]) - receipt_epoch
                if skew > MAX_FUTURE_SKEW_SECONDS:
                    raise FutureTimestampError(str(event["event_id"]), skew)

        accepted_ids: list[str] = []
        duplicate_ids: list[str] = []

        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                for _, timestamp_group_iterator in itertools.groupby(
                    normalized, key=lambda item: item["timestamp_epoch"]
                ):
                    timestamp_group = list(timestamp_group_iterator)
                    canonical_winners: dict[str, str] = {}
                    for event in timestamp_group:
                        robot_id = str(event["robot_id"])
                        event_id = str(event["event_id"])
                        canonical_winners[robot_id] = max(
                            event_id, canonical_winners.get(robot_id, event_id)
                        )
                    projected_group: list[dict[str, Any]] = []
                    for event in timestamp_group:
                        existing = self.connection.execute(
                            "SELECT * FROM telemetry WHERE event_id = ?",
                            (event["event_id"],),
                        ).fetchone()
                        if existing is not None:
                            if not self._same_event(existing, event):
                                raise EventIDConflict(str(event["event_id"]))
                            duplicate_ids.append(str(event["event_id"]))
                            continue
                        self._ensure_source_sequence_available(event)
                        is_canonical_winner = (
                            event["event_id"] == canonical_winners[str(event["robot_id"])]
                        )
                        projection_applied = is_canonical_winner and self._should_project(event)
                        self._insert_telemetry(
                            event,
                            receipt_at=receipt_at,
                            receipt_epoch=receipt_epoch,
                            projection_applied=projection_applied,
                        )
                        self.connection.execute(
                            """
                            INSERT INTO robot_revisions (robot_id, revision)
                            VALUES (?, 1)
                            ON CONFLICT(robot_id) DO UPDATE SET
                                revision = robot_revisions.revision + 1
                            """,
                            (event["robot_id"],),
                        )
                        accepted_ids.append(str(event["event_id"]))
                        if projection_applied:
                            self.connection.execute(
                                """
                                INSERT INTO robot_state (robot_id, event_id)
                                VALUES (?, ?)
                                ON CONFLICT(robot_id) DO UPDATE SET event_id = excluded.event_id
                                """,
                                (event["robot_id"], event["event_id"]),
                            )
                            projected_group.append(event)

                    if not projected_group:
                        continue
                    group_clock = (
                        str(projected_group[0]["timestamp"]),
                        float(projected_group[0]["timestamp_epoch"]),
                    )
                    if not trusted and group_clock[1] > receipt_epoch:
                        group_clock = (receipt_at, receipt_epoch)
                    if self.clock_mode == "virtual":
                        self._advance_virtual_clock(group_clock)
                    else:
                        self._advance_wall_clock((receipt_at, receipt_epoch))
                    for event in projected_group:
                        self._evaluate_metric_alerts(event)
                        self._evaluate_spatial_alerts(event)
                    self._evaluate_stale_alerts(
                        self._wall_clock() if self.clock_mode == "wall" else None
                    )

                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            if accepted_ids:
                self._revision += 1

        return {
            "accepted": len(accepted_ids),
            "duplicates": len(duplicate_ids),
            "event_ids": accepted_ids,
        }

    def _ensure_source_sequence_available(self, event: dict[str, Any]) -> None:
        if event["boot_id"] is None:
            return
        row = self.connection.execute(
            """
            SELECT event_id FROM telemetry
            WHERE robot_id = ? AND boot_id = ? AND sequence_number = ?
            """,
            (event["robot_id"], event["boot_id"], event["sequence_number"]),
        ).fetchone()
        if row is not None:
            raise SourceSequenceConflict(
                str(event["robot_id"]),
                str(event["boot_id"]),
                int(event["sequence_number"]),
            )

    def _should_project(self, event: dict[str, Any]) -> bool:
        current = self.connection.execute(
            """
            SELECT telemetry.*
            FROM robot_state
            JOIN telemetry USING (event_id)
            WHERE robot_state.robot_id = ?
            """,
            (event["robot_id"],),
        ).fetchone()
        if current is None:
            return True
        current_epoch = float(current["timestamp_epoch"])
        event_epoch = float(event["timestamp_epoch"])
        if event_epoch != current_epoch:
            return event_epoch > current_epoch
        return str(event["event_id"]) > str(current["event_id"])

    def _insert_telemetry(
        self,
        event: dict[str, Any],
        *,
        receipt_at: str,
        receipt_epoch: float,
        projection_applied: bool,
    ) -> None:
        pose = event["pose"]
        covariance = pose["covariance"] if pose is not None else None
        self.connection.execute(
            """
            INSERT INTO telemetry (
                event_id, robot_id, schema_version, timestamp, timestamp_epoch,
                boot_id, sequence_number, mission_id, operating_mode,
                battery_percentage, temperature, link_quality_percentage,
                pose_frame_id, pose_x_m, pose_y_m, pose_heading_deg,
                pose_speed_mps, covariance_xx, covariance_xy, covariance_yy,
                accuracy_95_m, received_at, received_epoch, projection_applied
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )
            """,
            (
                event["event_id"],
                event["robot_id"],
                event["schema_version"],
                event["timestamp"],
                event["timestamp_epoch"],
                event["boot_id"],
                event["sequence_number"],
                event["mission_id"],
                event["operating_mode"],
                event["battery_percentage"],
                event["temperature"],
                event["link_quality_percentage"],
                pose["frame_id"] if pose else None,
                pose["x_m"] if pose else None,
                pose["y_m"] if pose else None,
                pose["heading_deg"] if pose else None,
                pose["speed_mps"] if pose else None,
                covariance["xx"] if covariance else None,
                covariance["xy"] if covariance else None,
                covariance["yy"] if covariance else None,
                event["accuracy_95_m"],
                receipt_at,
                receipt_epoch,
                int(projection_applied),
            ),
        )

    def _active_alert(self, robot_id: str, alert_type: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM alerts
            WHERE robot_id = ? AND alert_type = ?
              AND status IN ('opened', 'acknowledged')
            ORDER BY id DESC LIMIT 1
            """,
            (robot_id, alert_type),
        ).fetchone()

    def _latest_alert(self, robot_id: str, alert_type: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM alerts
            WHERE robot_id = ? AND alert_type = ?
            ORDER BY id DESC LIMIT 1
            """,
            (robot_id, alert_type),
        ).fetchone()

    @staticmethod
    def _not_before(
        candidate_at: str,
        candidate_epoch: float,
        current: sqlite3.Row,
    ) -> tuple[str, float]:
        current_at = str(current["updated_at"])
        _, current_epoch = parse_timestamp(current_at)
        if candidate_epoch < current_epoch:
            return current_at, current_epoch
        return candidate_at, candidate_epoch

    @staticmethod
    def _severity(alert_type: str) -> str:
        return "critical" if alert_type in CRITICAL_ALERT_TYPES else "warning"

    def _insert_alert_event(
        self,
        *,
        alert_id: int,
        transition_type: str,
        occurred_at: str,
        occurred_epoch: float,
        actor: str,
        version: int,
        source_event_id: str | None,
        value: float | None,
        message: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO alert_events (
                alert_id, transition_type, occurred_at, occurred_epoch, actor,
                alert_version, source_event_id, value, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert_id,
                transition_type,
                occurred_at,
                occurred_epoch,
                actor,
                version,
                source_event_id,
                value,
                message,
            ),
        )

    def _transition_alert(
        self,
        *,
        robot_id: str,
        alert_type: str,
        should_open: bool,
        should_recover: bool,
        transition_at: str,
        transition_epoch: float,
        event_id: str | None,
        value: float,
        active_message: str,
        recovery_message: str,
    ) -> bool:
        current = self._active_alert(robot_id, alert_type)
        if current is None and should_open:
            previous = self._latest_alert(robot_id, alert_type)
            if previous is not None:
                transition_at, transition_epoch = self._not_before(
                    transition_at, transition_epoch, previous
                )
            cursor = self.connection.execute(
                """
                INSERT INTO alerts (
                    robot_id, alert_type, severity, status, version, opened_at,
                    opened_epoch, updated_at, last_event_id, value, message
                ) VALUES (?, ?, ?, 'opened', 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    robot_id,
                    alert_type,
                    self._severity(alert_type),
                    transition_at,
                    transition_epoch,
                    transition_at,
                    event_id,
                    value,
                    active_message,
                ),
            )
            if cursor.lastrowid is None:
                raise sqlite3.DatabaseError("incident insert returned no identifier")
            alert_id = int(cursor.lastrowid)
            self._insert_alert_event(
                alert_id=alert_id,
                transition_type="opened",
                occurred_at=transition_at,
                occurred_epoch=transition_epoch,
                actor="system",
                version=1,
                source_event_id=event_id,
                value=value,
                message=active_message,
            )
            return True
        if current is None:
            return False

        transition_at, transition_epoch = self._not_before(transition_at, transition_epoch, current)
        if should_recover:
            next_version = int(current["version"]) + 1
            self.connection.execute(
                """
                UPDATE alerts
                SET status = 'recovered', recovered_at = ?, updated_at = ?,
                    last_event_id = ?, value = ?, message = ?, version = ?
                WHERE id = ?
                """,
                (
                    transition_at,
                    transition_at,
                    event_id,
                    value,
                    recovery_message,
                    next_version,
                    current["id"],
                ),
            )
            self._insert_alert_event(
                alert_id=int(current["id"]),
                transition_type="recovered",
                occurred_at=transition_at,
                occurred_epoch=transition_epoch,
                actor="system",
                version=next_version,
                source_event_id=event_id,
                value=value,
                message=recovery_message,
            )
            return True

        # Do not churn wall-clock stale incident versions every evaluator pass.
        if event_id == current["last_event_id"] and alert_type == "stale_telemetry":
            return False
        self.connection.execute(
            """
            UPDATE alerts
            SET updated_at = ?, last_event_id = ?, value = ?, message = ?
            WHERE id = ?
            """,
            (
                transition_at,
                event_id,
                value,
                active_message,
                current["id"],
            ),
        )
        return True

    def _evaluate_metric_alerts(self, event: dict[str, Any]) -> None:
        battery = float(event["battery_percentage"])
        self._transition_alert(
            robot_id=str(event["robot_id"]),
            alert_type="low_battery",
            should_open=battery < self.low_battery_open,
            should_recover=battery >= self.low_battery_recover,
            transition_at=str(event["timestamp"]),
            transition_epoch=float(event["timestamp_epoch"]),
            event_id=str(event["event_id"]),
            value=battery,
            active_message=(
                f"Battery is low at {battery:g}%"
                if battery < self.low_battery_open
                else (
                    f"Battery is recovering at {battery:g}%; incident remains open until "
                    f"≥{self.low_battery_recover:g}%"
                )
            ),
            recovery_message=f"Battery recovered to {battery:g}%",
        )
        temperature = float(event["temperature"])
        self._transition_alert(
            robot_id=str(event["robot_id"]),
            alert_type="overheating",
            should_open=temperature > self.overheating_open,
            should_recover=temperature <= self.overheating_recover,
            transition_at=str(event["timestamp"]),
            transition_epoch=float(event["timestamp_epoch"]),
            event_id=str(event["event_id"]),
            value=temperature,
            active_message=(
                f"Temperature is high at {temperature:g}°C"
                if temperature > self.overheating_open
                else (
                    f"Cooling at {temperature:g}°C; incident remains open until "
                    f"≤{self.overheating_recover:g}°C"
                )
            ),
            recovery_message=f"Temperature recovered to {temperature:g}°C",
        )

    def _evaluate_spatial_alerts(self, event: dict[str, Any]) -> None:
        pose = event["pose"]
        if pose is None:
            return
        accuracy = float(event["accuracy_95_m"])
        self._transition_alert(
            robot_id=str(event["robot_id"]),
            alert_type="localization_degraded",
            should_open=accuracy > self.localization_open_metres,
            should_recover=accuracy <= self.localization_recover_metres,
            transition_at=str(event["timestamp"]),
            transition_epoch=float(event["timestamp_epoch"]),
            event_id=str(event["event_id"]),
            value=accuracy,
            active_message=(
                f"95% localization uncertainty grew to {accuracy:.2f} m"
                if accuracy > self.localization_open_metres
                else (
                    f"95% localization uncertainty is recovering at {accuracy:.2f} m; "
                    f"incident remains open until ≤{self.localization_recover_metres:.2f} m"
                )
            ),
            recovery_message=f"95% localization uncertainty recovered to {accuracy:.2f} m",
        )
        if self.site is None:
            return
        restricted_zone = self.site.restricted_zone_at(float(pose["x_m"]), float(pose["y_m"]))
        self._transition_alert(
            robot_id=str(event["robot_id"]),
            alert_type="geofence_breach",
            should_open=restricted_zone is not None,
            should_recover=restricted_zone is None,
            transition_at=str(event["timestamp"]),
            transition_epoch=float(event["timestamp_epoch"]),
            event_id=str(event["event_id"]),
            value=1.0 if restricted_zone else 0.0,
            active_message=f"Robot entered restricted zone {restricted_zone}",
            recovery_message="Robot left the restricted zone",
        )

    def _latest_rows(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT telemetry.*, robot_revisions.revision AS history_revision
            FROM robot_state
            JOIN telemetry USING (event_id)
            JOIN robot_revisions USING (robot_id)
            ORDER BY robot_id
            """
        ).fetchall()

    def _evaluate_stale_alerts(
        self,
        now: tuple[str, float] | None = None,
    ) -> bool:
        latest = self._latest_rows()
        if not latest:
            return False
        if self.clock_mode == "wall":
            clock_at, clock_epoch = now or self._wall_clock() or self._now()
            epoch_field = "received_epoch"
        else:
            virtual_clock = self._virtual_clock()
            if virtual_clock is None:
                fleet_latest = max(latest, key=lambda row: float(row["timestamp_epoch"]))
                virtual_clock = (
                    str(fleet_latest["timestamp"]),
                    float(fleet_latest["timestamp_epoch"]),
                )
            clock_at, clock_epoch = virtual_clock
            epoch_field = "timestamp_epoch"
        changed = False
        for row in latest:
            lag = max(0.0, clock_epoch - float(row[epoch_field]))
            changed = (
                self._transition_alert(
                    robot_id=str(row["robot_id"]),
                    alert_type="stale_telemetry",
                    should_open=lag > self.stale_after_seconds,
                    should_recover=lag <= self.stale_recover_seconds,
                    transition_at=clock_at,
                    transition_epoch=clock_epoch,
                    event_id=str(row["event_id"]),
                    value=lag,
                    active_message=f"Telemetry is stale by {lag:g} seconds",
                    recovery_message="Telemetry is current again",
                )
                or changed
            )
        return changed

    def evaluate_liveness(self, now: str | datetime | None = None) -> bool:
        """Advance stale incidents while a wall-clock fleet is silent."""

        if self.clock_mode != "wall":
            return False
        clock = parse_timestamp(now) if now is not None else self._now()
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                clock_advanced = self._advance_wall_clock(clock)
                changed = self._evaluate_stale_alerts(self._wall_clock())
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            if changed or clock_advanced:
                self._revision += 1
            return changed

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        pose: dict[str, Any] | None = None
        if row["pose_frame_id"] is not None:
            pose = {
                "frame_id": row["pose_frame_id"],
                "x_m": row["pose_x_m"],
                "y_m": row["pose_y_m"],
                "heading_deg": row["pose_heading_deg"],
                "speed_mps": row["pose_speed_mps"],
                "covariance": {
                    "xx": row["covariance_xx"],
                    "xy": row["covariance_xy"],
                    "yy": row["covariance_yy"],
                },
            }
        return {
            "event_id": row["event_id"],
            "robot_id": row["robot_id"],
            "timestamp": row["timestamp"],
            "received_at": row["received_at"],
            "mission_id": row["mission_id"],
            "operating_mode": row["operating_mode"],
            "battery_percentage": row["battery_percentage"],
            "temperature": row["temperature"],
            "link_quality_percentage": row["link_quality_percentage"],
            "pose": pose,
        }

    @staticmethod
    def _alert_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "robot_id": row["robot_id"],
            "type": row["alert_type"],
            "severity": row["severity"],
            "status": row["status"],
            "version": int(row["version"]),
            "opened_at": row["opened_at"],
            "acknowledged_at": row["acknowledged_at"],
            "recovered_at": row["recovered_at"],
            "updated_at": row["updated_at"],
            "value": row["value"],
            "message": row["message"],
        }

    def _fleet_health_from_rows(
        self,
        latest: list[sqlite3.Row],
        active_rows: list[sqlite3.Row],
    ) -> dict[str, Any]:
        alerts_by_robot: dict[str, list[dict[str, Any]]] = {}
        for row in active_rows:
            alerts_by_robot.setdefault(str(row["robot_id"]), []).append(self._alert_dict(row))
        robots: list[dict[str, Any]] = []
        counts = {"healthy": 0, "warning": 0, "critical": 0}
        stale_count = located = moving = degraded = missions = ready = 0
        for row in latest:
            active_alerts = alerts_by_robot.get(str(row["robot_id"]), [])
            types = {str(alert["type"]) for alert in active_alerts}
            is_stale = "stale_telemetry" in types
            if types.intersection(CRITICAL_ALERT_TYPES):
                status = "critical"
            elif types:
                status = "warning"
            else:
                status = "healthy"
            event = self._event_dict(row)
            pose = event["pose"]
            zone_id = None
            if pose is not None and self.site is not None:
                zone_id = self.site.zone_at(float(pose["x_m"]), float(pose["y_m"]))
            accuracy = row["accuracy_95_m"]
            localization_state = (
                "unavailable"
                if pose is None
                else "degraded"
                if "localization_degraded" in types
                else "nominal"
            )
            counts[status] += 1
            stale_count += int(is_stale)
            located += int(pose is not None)
            moving += int(pose is not None and float(pose["speed_mps"]) > 0.05)
            degraded += int(localization_state == "degraded")
            missions += int(event["mission_id"] is not None)
            ready += int(not is_stale and status != "critical" and pose is not None)
            robots.append(
                {
                    **event,
                    "status": status,
                    "stale": is_stale,
                    "active_alerts": active_alerts,
                    "history_revision": int(row["history_revision"]),
                    "zone_id": zone_id,
                    "route_id": (
                        self.site.route_for_robot(str(row["robot_id"]))
                        if self.site is not None
                        else None
                    ),
                    "localization_state": localization_state,
                    "accuracy_95_m": accuracy,
                }
            )
        as_of = None
        if latest:
            trusted_clock = (
                self._virtual_clock() if self.clock_mode == "virtual" else self._wall_clock()
            )
            as_of = (
                trusted_clock[0]
                if trusted_clock is not None
                else max(latest, key=lambda row: float(row["timestamp_epoch"]))["timestamp"]
            )
        return {
            "summary": {
                "total": len(robots),
                "healthy": counts["healthy"],
                "warning": counts["warning"],
                "critical": counts["critical"],
                "stale": stale_count,
                "active_alerts": len(active_rows),
                "ready": ready,
                "located": located,
                "moving": moving,
                "localization_degraded": degraded,
                "active_missions": missions,
            },
            "robots": robots,
            "as_of": as_of,
            "clock_mode": self.clock_mode,
        }

    def fleet_health(self) -> dict[str, Any]:
        with self._lock:
            latest = self._latest_rows()
            active_rows = self.connection.execute(
                """
                SELECT * FROM alerts
                WHERE status IN ('opened', 'acknowledged')
                ORDER BY opened_epoch, id
                """
            ).fetchall()
            return self._fleet_health_from_rows(latest, active_rows)

    def dashboard_snapshot(self) -> dict[str, Any]:
        """Return fleet health and bounded incidents from one locked view."""

        with self._lock:
            latest = self._latest_rows()
            active_rows = self.connection.execute(
                """
                SELECT * FROM alerts
                WHERE status IN ('opened', 'acknowledged')
                ORDER BY opened_epoch, id
                """
            ).fetchall()
            recent_rows = self.connection.execute(
                """
                SELECT * FROM alerts
                WHERE status = 'recovered'
                ORDER BY opened_epoch DESC, id DESC
                LIMIT 20
                """
            ).fetchall()
            incident_rows = sorted(
                [*active_rows, *recent_rows],
                key=lambda row: (float(row["opened_epoch"]), int(row["id"])),
                reverse=True,
            )
            fleet = self._fleet_health_from_rows(latest, active_rows)
            return {
                **fleet,
                "alerts": [self._alert_dict(row) for row in incident_rows],
                "revision": self._revision,
            }

    def robot_history(self, robot_id: str, limit: int) -> list[dict[str, Any]]:
        return self.robot_history_payload(robot_id, limit)["events"]

    def robot_history_payload(self, robot_id: str, limit: int) -> dict[str, Any]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM telemetry
                    WHERE robot_id = ?
                    ORDER BY timestamp_epoch DESC, event_id DESC
                    LIMIT ?
                )
                ORDER BY timestamp_epoch ASC, event_id ASC
                """,
                (robot_id, limit),
            ).fetchall()
            revision_row = self.connection.execute(
                """
                SELECT COALESCE(
                    (SELECT revision FROM robot_revisions WHERE robot_id = ?), 0
                ) AS revision
                """,
                (robot_id,),
            ).fetchone()
            events = [self._event_dict(row) for row in rows]
            return {
                "robot_id": robot_id,
                "events": events,
                "count": len(events),
                "history_revision": int(revision_row["revision"]),
            }

    def acknowledge_alert(
        self,
        alert_id: int,
        expected_version: int | None = None,
        actor: str = "operator",
    ) -> tuple[str, dict[str, Any] | None]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            changed = False
            try:
                row = self.connection.execute(
                    "SELECT * FROM alerts WHERE id = ?", (alert_id,)
                ).fetchone()
                if row is None:
                    self.connection.execute("COMMIT")
                    return "missing", None
                if expected_version is not None and int(row["version"]) != expected_version:
                    self.connection.execute("COMMIT")
                    return "conflict", self._alert_dict(row)
                if row["status"] == "recovered":
                    self.connection.execute("COMMIT")
                    return "recovered", self._alert_dict(row)
                if row["status"] == "opened":
                    if self.clock_mode == "wall":
                        acknowledge_at, acknowledge_epoch = self._now()
                    else:
                        fleet_row = self.connection.execute(
                            """
                            SELECT timestamp, timestamp_epoch
                            FROM robot_state JOIN telemetry USING (event_id)
                            ORDER BY timestamp_epoch DESC, event_id DESC LIMIT 1
                            """
                        ).fetchone()
                        if fleet_row is None:
                            acknowledge_at = str(row["updated_at"])
                            _, acknowledge_epoch = parse_timestamp(acknowledge_at)
                        else:
                            acknowledge_at = str(fleet_row["timestamp"])
                            acknowledge_epoch = float(fleet_row["timestamp_epoch"])
                    acknowledge_at, acknowledge_epoch = self._not_before(
                        acknowledge_at, acknowledge_epoch, row
                    )
                    next_version = int(row["version"]) + 1
                    message = f"Incident acknowledged by {actor}"
                    self.connection.execute(
                        """
                        UPDATE alerts
                        SET status = 'acknowledged', acknowledged_at = ?,
                            updated_at = ?, version = ?
                        WHERE id = ?
                        """,
                        (acknowledge_at, acknowledge_at, next_version, alert_id),
                    )
                    self._insert_alert_event(
                        alert_id=alert_id,
                        transition_type="acknowledged",
                        occurred_at=acknowledge_at,
                        occurred_epoch=acknowledge_epoch,
                        actor=actor,
                        version=next_version,
                        source_event_id=None,
                        value=row["value"],
                        message=message,
                    )
                    changed = True
                updated = self.connection.execute(
                    "SELECT * FROM alerts WHERE id = ?", (alert_id,)
                ).fetchone()
                payload = self._alert_dict(updated)
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            if changed:
                self._revision += 1
        return "ok", payload

    def clear(self) -> None:
        """Remove scenario state without ever reusing incident identifiers."""

        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute("DELETE FROM alert_events")
                self.connection.execute("DELETE FROM alerts")
                self.connection.execute("DELETE FROM robot_state")
                self.connection.execute("DELETE FROM telemetry")
                self.connection.execute(
                    """
                    DELETE FROM application_metadata
                    WHERE key IN ('virtual_clock_at', 'wall_clock_at')
                    """
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            self._revision += 1
