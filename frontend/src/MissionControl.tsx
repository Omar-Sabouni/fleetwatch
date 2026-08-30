import { useEffect, useMemo, useState } from "react";

import { FacilityMap } from "./FacilityMap";
import { formatTimestamp } from "./time";
import type {
  Alert,
  DashboardSnapshot,
  HistoryEvent,
  Robot,
  SiteConfig,
} from "./types";
import { runVersionedAction, useDashboardFeed, useRobotHistory } from "./useFleetWatch";
import type { RobotHistoryState } from "./useFleetWatch";

type ReplayAction = "restart" | "start" | "pause" | "step";

const INCIDENTS: Record<string, { code: string; label: string }> = {
  geofence_breach: { code: "ZONE", label: "Restricted zone" },
  overheating: { code: "TEMP", label: "Motor temperature" },
  stale_telemetry: { code: "LINK", label: "Telemetry stale" },
  localization_degraded: { code: "LOC", label: "Position uncertainty" },
  low_battery: { code: "BATT", label: "Low battery" },
};

function incidentInfo(alert: Alert): { code: string; label: string } {
  return INCIDENTS[alert.type] ?? {
    code: alert.type.slice(0, 4).toUpperCase(),
    label: alert.type.replaceAll("_", " "),
  };
}


function robotName(site: SiteConfig, robotId: string): string {
  return site.robots.find((robot) => robot.id === robotId)?.displayName ?? robotId.toUpperCase();
}

function shortRobotName(site: SiteConfig, robotId: string): string {
  return robotName(site, robotId).replace(/^DXB-/, "").replace(/^ROBOT-/i, "R");
}

function zoneName(site: SiteConfig, zoneId: string | null): string {
  if (zoneId === null) return "No zone";
  return site.zones.find((zone) => zone.id === zoneId)?.name ?? zoneId.replaceAll("-", " ");
}

function activeAlerts(alerts: Alert[]): Alert[] {
  return alerts
    .filter((alert) => alert.status !== "recovered")
    .sort(
      (left, right) =>
        (left.severity === "critical" ? 0 : left.severity === "warning" ? 1 : 2) -
          (right.severity === "critical" ? 0 : right.severity === "warning" ? 1 : 2) ||
        Date.parse(right.openedAt ?? "") - Date.parse(left.openedAt ?? ""),
    );
}

function robotRank(robot: Robot, alerts: Alert[]): number {
  const related = alerts.filter((alert) => alert.robotId === robot.id);
  if (related.some((alert) => alert.severity === "critical")) return 0;
  if (robot.stale) return 1;
  if (related.length > 0 || robot.status === "warning") return 2;
  if (robot.status === "unknown") return 3;
  return 4;
}

function fleetAge(robot: Robot, snapshot: DashboardSnapshot): string {
  const source = snapshot.clockMode === "wall" ? robot.receivedAt : robot.timestamp;
  const event = source === null ? Number.NaN : Date.parse(source);
  const fleet = snapshot.fleetTime === null ? Number.NaN : Date.parse(snapshot.fleetTime);
  if (!Number.isFinite(event) || !Number.isFinite(fleet)) return "—";
  const seconds = Math.max(0, Math.round((fleet - event) / 1_000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.round(seconds / 60)}m`;
}

function SystemBar({
  site,
  snapshot,
  connection,
  siteIsFallback,
}: {
  site: SiteConfig;
  snapshot: DashboardSnapshot | null;
  connection: "connecting" | "connected" | "offline";
  siteIsFallback: boolean;
}): React.JSX.Element {
  return (
    <header className="system-bar">
      <div className="site-id">
        <h1>FleetWatch <span className="site-code">{site.id.toUpperCase()}</span></h1>
      </div>
      <time dateTime={snapshot?.fleetTime ?? ""}>{formatTimestamp(snapshot?.fleetTime ?? null)}</time>
      <span className="connection-state" data-state={siteIsFallback ? "offline" : connection}>
        {siteIsFallback ? "Site data offline" : connection === "connected" ? "Live" : connection}
      </span>
    </header>
  );
}

function FleetLedger({
  site,
  snapshot,
  selectedRobotId,
  onSelect,
}: {
  site: SiteConfig;
  snapshot: DashboardSnapshot;
  selectedRobotId: string | null;
  onSelect: (robotId: string) => void;
}): React.JSX.Element {
  const open = activeAlerts(snapshot.alerts);
  const robots = useMemo(
    () =>
      [...snapshot.robots].sort(
        (left, right) => robotRank(left, open) - robotRank(right, open) || left.id.localeCompare(right.id),
      ),
    [open, snapshot.robots],
  );
  return (
    <section className="ledger" aria-labelledby="fleet-heading">
      <header className="pane-heading">
        <div>
          <h2 id="fleet-heading">Robot roster</h2>
          <span>{snapshot.summary.ready} of {snapshot.summary.total} ready</span>
        </div>
      </header>
      <div className="table-scroll">
        <table className="fleet-table">
          <thead>
            <tr>
              <th>Robot</th>
              <th>Battery</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            {robots.map((robot) => {
              const related = open.filter((alert) => alert.robotId === robot.id);
              const issue = related[0];
              return (
                <tr
                  key={robot.id}
                  data-status={issue?.severity ?? (robot.stale ? "warning" : robot.status)}
                  data-selected={String(robot.id === selectedRobotId)}
                >
                  <th scope="row">
                    <button
                      type="button"
                      aria-pressed={robot.id === selectedRobotId}
                      onClick={() => onSelect(robot.id)}
                    >
                      {shortRobotName(site, robot.id)}
                    </button>
                  </th>
                  <td>{robot.battery === null ? "—" : `${robot.battery.toFixed(0)}%`}</td>
                  <td className="state-code">
                    {issue ? incidentInfo(issue).label : robot.stale ? "Telemetry stale" : "OK"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function AttentionQueue({
  site,
  alerts,
  onSelect,
}: {
  site: SiteConfig;
  alerts: Alert[];
  onSelect: (robotId: string) => void;
}): React.JSX.Element {
  const open = activeAlerts(alerts);
  return (
    <section className="attention-queue" aria-labelledby="attention-heading">
      <header>
        <h2 id="attention-heading">Incidents</h2>
        <span>{open.length}</span>
      </header>
      {open.length === 0 ? <p className="all-clear">Nothing open.</p> : null}
      <ol>
        {open.map((alert) => (
          <li key={alert.id} data-severity={alert.severity}>
            <button type="button" onClick={() => onSelect(alert.robotId)}>
              <span className="severity-dot" aria-label={alert.severity === "critical" ? "Critical" : "Warning"} />
              <strong>{shortRobotName(site, alert.robotId)}</strong>
              <span>{incidentInfo(alert).label}</span>
            </button>
          </li>
        ))}
      </ol>
    </section>
  );
}

function Trend({
  label,
  unit,
  events,
  value,
}: {
  label: string;
  unit: string;
  events: HistoryEvent[];
  value: (event: HistoryEvent) => number | null;
}): React.JSX.Element {
  const samples = events.map(value).filter((sample): sample is number => sample !== null);
  const minimum = samples.length > 0 ? Math.min(...samples) : 0;
  const maximum = samples.length > 0 ? Math.max(...samples) : 0;
  const range = Math.max(maximum - minimum, 1);
  const points = samples
    .map((sample, index) => {
      const x = samples.length < 2 ? 100 : (index / (samples.length - 1)) * 100;
      const y = 21 - ((sample - minimum) / range) * 17;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
  const latest = samples.at(-1);
  return (
    <div className="trend-row">
      <div>
        <span>{label}</span>
        <strong>{latest === undefined ? "—" : `${latest.toFixed(label === "Position error" ? 2 : 0)}${unit}`}</strong>
      </div>
      <svg viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true">
        <line x1="0" x2="100" y1="21" y2="21" />
        {points ? <polyline points={points} /> : null}
      </svg>
    </div>
  );
}

function reading(value: number | null, unit: string, digits = 0): string {
  return value === null ? "—" : `${value.toFixed(digits)}${unit}`;
}

function AssetPane({
  robot,
  site,
  snapshot,
  historyState,
  pending,
  readOnly,
  onAcknowledge,
}: {
  robot: Robot | null;
  site: SiteConfig;
  snapshot: DashboardSnapshot;
  historyState: RobotHistoryState;
  pending: string | null;
  readOnly: boolean;
  onAcknowledge: (alert: Alert) => void;
}): React.JSX.Element {
  if (robot === null) {
    return <aside className="asset-pane empty-asset">Select a fleet unit</aside>;
  }
  const alerts = activeAlerts(snapshot.alerts).filter((alert) => alert.robotId === robot.id);
  const history = historyState.history?.robotId === robot.id ? historyState.history : null;
  const route = site.routes.find((item) => item.id === robot.routeId)?.name ?? "Unassigned";
  return (
    <aside className="asset-pane" aria-labelledby="asset-heading">
      <header className="asset-heading" data-status={alerts[0]?.severity ?? robot.status}>
        <div>
          <h2 id="asset-heading">{robotName(site, robot.id)}</h2>
          <span>{robot.mode.replaceAll("_", " ")} in {zoneName(site, robot.zoneId)}, on {route}</span>
        </div>
      </header>

      <section className="asset-alarms" aria-labelledby="asset-alarms-heading">
        <h3 id="asset-alarms-heading" className="visually-hidden">Incidents</h3>
        {alerts.length === 0 ? <p className="all-clear">Nothing wrong.</p> : null}
        {alerts.map((alert) => (
          <article key={alert.id} data-severity={alert.severity}>
            <header>
              <strong>{incidentInfo(alert).label}</strong>
              <time dateTime={alert.openedAt ?? ""}>{formatTimestamp(alert.openedAt)}</time>
            </header>
            <p>{alert.message}</p>
            {alert.status === "opened" && !readOnly ? (
              <button type="button" disabled={pending !== null} onClick={() => onAcknowledge(alert)}>
                {pending === `ack-${alert.id}` ? "Acknowledging…" : "Acknowledge"}
              </button>
            ) : (
              <span className="alarm-state">{alert.status}</span>
            )}
          </article>
        ))}
      </section>

      <section className="trends" aria-labelledby="trends-heading">
        <h3 id="trends-heading">Telemetry</h3>
        {history ? (
          <>
            <Trend label="Battery" unit="%" events={history.events} value={(event) => event.battery} />
            <Trend label="Motor" unit="°C" events={history.events} value={(event) => event.temperature} />
            <Trend label="Position error" unit=" m" events={history.events} value={(event) => event.accuracy95Meters} />
          </>
        ) : historyState.error ? (
          <p className="history-error">History unavailable. <button type="button" onClick={historyState.retry}>Retry</button></p>
        ) : (
          <p className="history-loading">{historyState.loading ? "Loading…" : "No history yet."}</p>
        )}
        <p className="signal-line">Last heard {fleetAge(robot, snapshot)} ago, link {reading(robot.linkQuality, "%")}.</p>
      </section>
    </aside>
  );
}

function ReplayBar({
  snapshot,
  site,
  pending,
  onAction,
}: {
  snapshot: DashboardSnapshot;
  site: SiteConfig;
  pending: string | null;
  onAction: (action: ReplayAction, speed: number) => void;
}): React.JSX.Element | null {
  const [speed, setSpeed] = useState(snapshot.replay.speed || site.scenario.defaultTicksPerSecond);
  if (!site.shiftControlsEnabled) return null;
  const readOnly = site.deployment.readOnly;
  return (
    <footer className="replay-bar">
      <strong>Shift</strong>
      <span>{site.scenario.label}</span>
      <span className="replay-count">Update {snapshot.replay.current}/{snapshot.replay.total || site.scenario.totalTicks}</span>
      <progress max="100" value={snapshot.replay.progress} aria-label="Shift progress" />
      {readOnly ? (
        <span className="replay-state">{snapshot.replay.state}</span>
      ) : (
        <div className="replay-controls">
          <label>Rate
            <select value={speed} onChange={(event) => setSpeed(Number(event.currentTarget.value))}>
              {[0.5, 1, 2, 4, 8].map((value) => <option key={value} value={value}>{value}×</option>)}
            </select>
          </label>
          <button type="button" disabled={pending !== null} onClick={() => onAction("restart", speed)}>Restart</button>
          <button
            type="button"
            disabled={pending !== null || snapshot.replay.state === "complete"}
            onClick={() => onAction(snapshot.replay.state === "running" ? "pause" : "start", speed)}
          >
            {snapshot.replay.state === "running" ? "Pause" : "Run"}
          </button>
          <button
            type="button"
            disabled={pending !== null || snapshot.replay.state === "running" || snapshot.replay.state === "complete"}
            onClick={() => onAction("step", speed)}
          >Step</button>
        </div>
      )}
    </footer>
  );
}

export function MissionControl(): React.JSX.Element {
  const feed = useDashboardFeed();
  const [selectedRobotId, setSelectedRobotId] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const snapshot = feed.snapshot;
  const selectedRobot = snapshot?.robots.find((robot) => robot.id === selectedRobotId) ?? null;
  const history = useRobotHistory(selectedRobot?.id ?? null, selectedRobot?.historyRevision ?? null);
  const readOnly = feed.siteIsFallback || feed.site.deployment.readOnly;

  useEffect(() => {
    if (snapshot === null || snapshot.robots.length === 0) return;
    if (snapshot.robots.some((robot) => robot.id === selectedRobotId)) return;
    const open = activeAlerts(snapshot.alerts);
    const first = [...snapshot.robots].sort(
      (left, right) => robotRank(left, open) - robotRank(right, open) || left.id.localeCompare(right.id),
    )[0];
    setSelectedRobotId(first?.id ?? null);
  }, [selectedRobotId, snapshot]);

  const selectRobot = (robotId: string): void => {
    setSelectedRobotId(robotId);
    setAnnouncement(`${robotId} selected.`);
  };

  const performReplayAction = async (action: ReplayAction, speed: number): Promise<void> => {
    if (snapshot === null || readOnly || pending !== null) return;
    setPending(action);
    setActionError(null);
    try {
      await runVersionedAction(
        `/api/replay/${action}`,
        snapshot.replay.revision,
        action === "start" || action === "restart" ? { ticks_per_second: speed } : undefined,
      );
      setAnnouncement(`Shift ${action} complete.`);
      feed.refresh();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Shift action failed.");
    } finally {
      setPending(null);
    }
  };

  const acknowledge = async (alert: Alert): Promise<void> => {
    if (readOnly || pending !== null) return;
    setPending(`ack-${alert.id}`);
    setActionError(null);
    try {
      await runVersionedAction(`/api/alerts/${encodeURIComponent(alert.id)}/acknowledge`, alert.version);
      setAnnouncement(`${incidentInfo(alert).label} acknowledged.`);
      feed.refresh();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Acknowledgement failed.");
    } finally {
      setPending(null);
    }
  };

  const visibleError = actionError ?? feed.error;
  return (
    <div className="fleetwatch-shell">
      <a className="skip-link" href="#workspace">Skip to fleet workspace</a>
      <SystemBar
        site={feed.site}
        snapshot={snapshot}
        connection={feed.connection}
        siteIsFallback={feed.siteIsFallback}
      />
      {visibleError ? (
        <div className="error-strip" role="alert">
          <strong>Feed error</strong><span>{visibleError}</span>
          <button type="button" onClick={() => { setActionError(null); feed.clearError(); }}>Dismiss</button>
        </div>
      ) : null}
      {snapshot === null ? (
        <main id="workspace" className="loading-view"><p>Waiting for first fleet snapshot…</p></main>
      ) : (
        <>
          <main id="workspace" className="workspace">
            <aside className="fleet-pane">
              <FleetLedger site={feed.site} snapshot={snapshot} selectedRobotId={selectedRobotId} onSelect={selectRobot} />
              <AttentionQueue site={feed.site} alerts={snapshot.alerts} onSelect={selectRobot} />
            </aside>
            <FacilityMap
              site={feed.site}
              robots={snapshot.robots}
              selectedRobotId={selectedRobotId}
              history={history.history}
              onSelectRobot={selectRobot}
            />
            <AssetPane
              robot={selectedRobot}
              site={feed.site}
              snapshot={snapshot}
              historyState={history}
              pending={pending}
              readOnly={readOnly}
              onAcknowledge={(alert) => void acknowledge(alert)}
            />
          </main>
          <ReplayBar
            snapshot={snapshot}
            site={feed.site}
            pending={pending}
            onAction={(action, speed) => void performReplayAction(action, speed)}
          />
        </>
      )}
      <div className="visually-hidden" role="status" aria-live="polite">{announcement}</div>
    </div>
  );
}
