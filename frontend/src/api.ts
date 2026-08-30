import type {
  Alert,
  AlertSeverity,
  AlertStatus,
  DashboardSnapshot,
  HistoryEvent,
  Point,
  ReplayState,
  Robot,
  RobotHistory,
  RobotPose,
  RobotStatus,
  SiteConfig,
  SiteZone,
} from "./types";

type JsonObject = Record<string, unknown>;

function object(value: unknown, label = "response"): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`Invalid ${label}.`);
  }
  return value as JsonObject;
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : fallback;
}

function number(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function nullableText(value: unknown): string | null {
  const result = text(value).trim();
  return result ? result : null;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function oneOf<Value extends string>(value: unknown, choices: readonly Value[], fallback: Value): Value {
  return choices.includes(value as Value) ? (value as Value) : fallback;
}

function point(value: unknown): Point {
  const source = object(value, "point");
  return { x: number(source.x_m ?? source.x), y: number(source.y_m ?? source.y) };
}

function pose(value: unknown): RobotPose | null {
  if (value === null || value === undefined) return null;
  const source = object(value, "pose");
  const covariance = object(source.covariance, "pose covariance");
  return {
    frameId: text(source.frame_id),
    x: number(source.x_m),
    y: number(source.y_m),
    headingDegrees: number(source.heading_deg),
    speedMetersPerSecond: number(source.speed_mps),
    covariance: {
      xx: number(covariance.xx),
      xy: number(covariance.xy),
      yy: number(covariance.yy),
    },
  };
}

function robot(value: unknown): Robot {
  const source = object(value, "robot");
  return {
    id: text(source.robot_id),
    timestamp: nullableText(source.timestamp),
    receivedAt: nullableText(source.received_at),
    mode: text(source.operating_mode, "unknown"),
    battery: nullableNumber(source.battery_percentage),
    temperature: nullableNumber(source.temperature),
    status: oneOf<RobotStatus>(
      source.status,
      ["healthy", "warning", "critical", "unknown"],
      "unknown",
    ),
    stale: source.stale === true,
    historyRevision: text(source.history_revision, "0"),
    missionId: nullableText(source.mission_id),
    linkQuality: nullableNumber(source.link_quality_percentage),
    pose: pose(source.pose),
    zoneId: nullableText(source.zone_id),
    routeId: nullableText(source.route_id),
    localizationState: oneOf(
      source.localization_state,
      ["nominal", "degraded", "unavailable"] as const,
      "unavailable",
    ),
    accuracy95Meters: nullableNumber(source.accuracy_95_m),
  };
}

function alert(value: unknown): Alert {
  const source = object(value, "incident");
  return {
    id: text(source.id),
    robotId: text(source.robot_id),
    type: text(source.type, "unknown"),
    status: oneOf<AlertStatus>(
      source.status,
      ["opened", "acknowledged", "recovered"],
      "opened",
    ),
    openedAt: nullableText(source.opened_at),
    message: text(source.message),
    value: nullableNumber(source.value),
    severity: oneOf<AlertSeverity>(
      source.severity,
      ["info", "warning", "critical"],
      "warning",
    ),
    version: text(source.version, "0"),
  };
}

export function normalizeDashboard(value: unknown): DashboardSnapshot {
  const source = object(value, "dashboard");
  const summary = object(source.summary, "fleet summary");
  const replay = object(source.replay, "replay state");
  return {
    revision: text(source.revision, "0"),
    fleetTime: nullableText(source.fleet_time),
    summary: {
      total: number(summary.total),
      healthy: number(summary.healthy),
      warning: number(summary.warning),
      critical: number(summary.critical),
      stale: number(summary.stale),
      activeAlerts: number(summary.active_alerts),
      ready: number(summary.ready),
      moving: number(summary.moving),
    },
    robots: list(source.robots).map(robot),
    alerts: list(source.alerts).map(alert),
    replay: {
      state: oneOf<ReplayState>(
        replay.state ?? replay.status,
        ["paused", "running", "complete", "error"],
        "error",
      ),
      current: number(replay.current_tick ?? replay.current),
      total: number(replay.total_ticks ?? replay.total),
      progress: number(replay.progress),
      speed: number(replay.ticks_per_second ?? replay.events_per_second, 1),
      revision: text(replay.revision, "0"),
    },
    clockMode: oneOf(source.clock_mode, ["virtual", "wall"] as const, "virtual"),
  };
}

function historyEvent(value: unknown): HistoryEvent {
  const source = object(value, "history event");
  return {
    eventId: text(source.event_id),
    timestamp: text(source.timestamp),
    mode: text(source.operating_mode, "unknown"),
    battery: number(source.battery_percentage),
    temperature: number(source.temperature),
    pose: pose(source.pose),
    accuracy95Meters: nullableNumber(source.accuracy_95_m),
    linkQuality: nullableNumber(source.link_quality_percentage),
  };
}

function zoneKind(value: unknown): SiteZone["kind"] {
  return oneOf(
    value,
    ["floor", "aisle", "staging", "dispatch", "charging", "inspection", "restricted", "other"] as const,
    "other",
  );
}

export function normalizeSite(value: unknown): SiteConfig {
  const source = object(value, "site");
  const bounds = object(source.bounds, "site bounds");
  const policies = object(source.policies, "site policies");
  const lowBattery = object(policies.low_battery, "low battery policy");
  const overheating = object(policies.overheating, "temperature policy");
  const stale = object(policies.stale_telemetry, "stale policy");
  const localization = object(policies.localization_degraded, "localization policy");
  const scenario = object(source.scenario, "scenario");
  const deployment = object(source.deployment, "deployment");
  return {
    id: text(source.site_id, "unknown-site"),
    name: text(source.name, "Facility"),
    frameId: text(source.frame_id, "map"),
    shiftControlsEnabled: source.shift_controls_enabled === true,
    bounds: { width: number(bounds.width_m, 120), height: number(bounds.height_m, 80) },
    zones: list(source.zones).map((value) => {
      const item = object(value, "zone");
      return {
        id: text(item.id),
        name: text(item.name),
        kind: zoneKind(item.kind),
        polygon: list(item.polygon).map(point),
      };
    }),
    anchors: list(source.anchors).map((value) => {
      const item = object(value, "anchor");
      return { id: text(item.id), ...point(item) };
    }),
    routes: list(source.routes).map((value) => {
      const item = object(value, "route");
      return { id: text(item.id), name: text(item.name), points: list(item.points).map(point) };
    }),
    robots: list(source.robots).map((value) => {
      const item = object(value, "site robot");
      return {
        id: text(item.id),
        displayName: text(item.display_name, text(item.id)),
        model: text(item.model, "AMR"),
        routeId: nullableText(item.route_id),
      };
    }),
    policies: {
      lowBattery: { open: number(lowBattery.open, 20), recover: number(lowBattery.recover, 25) },
      overheating: { open: number(overheating.open, 75), recover: number(overheating.recover, 70) },
      staleTelemetry: {
        openSeconds: number(stale.open_seconds, 120),
        recoverSeconds: number(stale.recover_seconds, 120),
      },
      localizationDegraded: {
        openMeters: number(localization.open_m, 3),
        recoverMeters: number(localization.recover_m, 2),
      },
    },
    scenario: {
      label: text(scenario.label, "Scenario"),
      defaultTicksPerSecond: number(scenario.default_ticks_per_second, 2),
      totalTicks: number(scenario.total_ticks, 0),
    },
    deployment: {
      readOnly: deployment.read_only !== false,
    },
  };
}

export const FALLBACK_SITE: SiteConfig = {
  id: "dxb-01",
  name: "Site unavailable",
  frameId: "dxb-01/map",
  shiftControlsEnabled: true,
  bounds: { width: 120, height: 80 },
  zones: [],
  anchors: [],
  routes: [],
  robots: [],
  policies: {
    lowBattery: { open: 20, recover: 25 },
    overheating: { open: 75, recover: 70 },
    staleTelemetry: { openSeconds: 120, recoverSeconds: 120 },
    localizationDegraded: { openMeters: 3, recoverMeters: 2 },
  },
  scenario: { label: "Morning shift", defaultTicksPerSecond: 2, totalTicks: 0 },
  deployment: { readOnly: true },
};

async function responseError(response: Response): Promise<string> {
  try {
    const payload = object((await response.json()) as unknown);
    return text(payload.detail ?? payload.message, `${response.status} ${response.statusText}`);
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

async function get(path: string, signal?: AbortSignal): Promise<unknown> {
  const response = await fetch(path, {
    signal: signal ?? null,
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(await responseError(response));
  return (await response.json()) as unknown;
}

export async function requestDashboard(signal?: AbortSignal): Promise<DashboardSnapshot> {
  return normalizeDashboard(await get("/api/dashboard", signal));
}

export async function requestSite(signal?: AbortSignal): Promise<SiteConfig> {
  return normalizeSite(await get("/api/site", signal));
}

export async function requestHistory(robotId: string, signal?: AbortSignal): Promise<RobotHistory> {
  const source = object(
    await get(`/api/robots/${encodeURIComponent(robotId)}/history?limit=80`, signal),
    "history",
  );
  return {
    robotId: text(source.robot_id, robotId),
    revision: text(source.history_revision, "0"),
    events: list(source.events).map(historyEvent),
  };
}

export async function postJsonVersioned(
  path: string,
  revision: string,
  body?: Record<string, unknown>,
): Promise<void> {
  const headers = new Headers({ Accept: "application/json", "If-Match": `"${revision}"` });
  const init: RequestInit = { method: "POST", headers };
  if (body !== undefined) {
    headers.set("Content-Type", "application/json");
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  if (!response.ok) throw new Error(await responseError(response));
}
