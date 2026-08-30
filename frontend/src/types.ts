export type RobotStatus = "healthy" | "warning" | "critical" | "unknown";
export type AlertStatus = "opened" | "acknowledged" | "recovered";
export type AlertSeverity = "info" | "warning" | "critical";
export type ReplayState = "paused" | "running" | "complete" | "error";
export type ConnectionState = "connecting" | "connected" | "offline";

export interface Point {
  x: number;
  y: number;
}

export interface RobotPose extends Point {
  frameId: string;
  headingDegrees: number;
  speedMetersPerSecond: number;
  covariance: { xx: number; xy: number; yy: number };
}

export interface Robot {
  id: string;
  timestamp: string | null;
  receivedAt: string | null;
  mode: string;
  battery: number | null;
  temperature: number | null;
  status: RobotStatus;
  stale: boolean;
  historyRevision: string;
  missionId: string | null;
  linkQuality: number | null;
  pose: RobotPose | null;
  zoneId: string | null;
  routeId: string | null;
  localizationState: "nominal" | "degraded" | "unavailable";
  accuracy95Meters: number | null;
}

export interface Alert {
  id: string;
  robotId: string;
  type: string;
  status: AlertStatus;
  openedAt: string | null;
  message: string;
  value: number | null;
  severity: AlertSeverity;
  version: string;
}

export interface DashboardSnapshot {
  revision: string;
  fleetTime: string | null;
  summary: {
    total: number;
    healthy: number;
    warning: number;
    critical: number;
    stale: number;
    activeAlerts: number;
    ready: number;
    moving: number;
  };
  robots: Robot[];
  alerts: Alert[];
  replay: {
    state: ReplayState;
    current: number;
    total: number;
    progress: number;
    speed: number;
    revision: string;
  };
  clockMode: "virtual" | "wall";
}

export interface HistoryEvent {
  eventId: string;
  timestamp: string;
  mode: string;
  battery: number;
  temperature: number;
  pose: RobotPose | null;
  accuracy95Meters: number | null;
  linkQuality: number | null;
}

export interface RobotHistory {
  robotId: string;
  revision: string;
  events: HistoryEvent[];
}

export interface SiteZone {
  id: string;
  name: string;
  kind:
    | "floor"
    | "aisle"
    | "staging"
    | "dispatch"
    | "charging"
    | "inspection"
    | "restricted"
    | "other";
  polygon: Point[];
}

export interface SiteConfig {
  id: string;
  name: string;
  frameId: string;
  shiftControlsEnabled: boolean;
  bounds: { width: number; height: number };
  zones: SiteZone[];
  anchors: (Point & { id: string })[];
  routes: { id: string; name: string; points: Point[] }[];
  robots: { id: string; displayName: string; model: string; routeId: string | null }[];
  policies: {
    lowBattery: { open: number; recover: number };
    overheating: { open: number; recover: number };
    staleTelemetry: { openSeconds: number; recoverSeconds: number };
    localizationDegraded: { openMeters: number; recoverMeters: number };
  };
  scenario: {
    label: string;
    defaultTicksPerSecond: number;
    totalTicks: number;
  };
  deployment: {
    readOnly: boolean;
  };
}
