import { useMemo } from "react";

import type { Robot, RobotHistory, RobotPose, SiteConfig } from "./types";

interface FacilityMapProps {
  site: SiteConfig;
  robots: Robot[];
  selectedRobotId: string | null;
  history: RobotHistory | null;
  onSelectRobot: (robotId: string) => void;
}

function coordinates(points: { x: number; y: number }[], site: SiteConfig): string {
  return points.map((point) => `${point.x},${site.bounds.height - point.y}`).join(" ");
}

function routePath(points: { x: number; y: number }[], site: SiteConfig): string {
  return points
    .map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${site.bounds.height - point.y}`)
    .join(" ");
}

function uncertainty(pose: RobotPose): { major: number; minor: number; angle: number } {
  const { xx, xy, yy } = pose.covariance;
  const delta = Math.sqrt(Math.max(0, (xx - yy) ** 2 + 4 * xy ** 2));
  return {
    major: Math.sqrt(Math.max(0, (xx + yy + delta) / 2) * 5.991),
    minor: Math.sqrt(Math.max(0, (xx + yy - delta) / 2) * 5.991),
    angle: (-Math.atan2(2 * xy, xx - yy) * 90) / Math.PI,
  };
}

function shortId(id: string): string {
  return id.replace("robot-", "R").replace("DXB-AMR-", "R");
}

export function FacilityMap({
  site,
  robots,
  selectedRobotId,
  history,
  onSelectRobot,
}: FacilityMapProps): React.JSX.Element {
  const located = robots.filter((robot): robot is Robot & { pose: RobotPose } => robot.pose !== null);
  const selected = located.find((robot) => robot.id === selectedRobotId) ?? null;
  const selectedRouteId =
    selected?.routeId ?? site.robots.find((robot) => robot.id === selectedRobotId)?.routeId;
  const selectedRoute = site.routes.find((route) => route.id === selectedRouteId);
  const trail = useMemo(
    () =>
      history?.robotId === selectedRobotId
        ? history.events
            .map((event) => event.pose)
            .filter((pose): pose is RobotPose => pose !== null)
        : [],
    [history, selectedRobotId],
  );

  return (
    <section className="map-pane" aria-labelledby="map-title">
      <h2 id="map-title" className="visually-hidden">Floor plan</h2>
      <div className="facility-map" style={{ aspectRatio: `${site.bounds.width} / ${site.bounds.height}` }}>
        {site.zones.length === 0 ? (
          <p className="map-unavailable">Site geometry unavailable</p>
        ) : null}
        <svg
          viewBox={`0 0 ${site.bounds.width} ${site.bounds.height}`}
          role="img"
          aria-label={`${site.name} floor plan with ${located.length} robot positions`}
          preserveAspectRatio="none"
        >
          <defs>
            <pattern id="hazard-tape" width="1.4" height="1.4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <rect width="1.4" height="1.4" fill="#f2c200" />
              <rect width="0.7" height="1.4" fill="#1e2225" />
            </pattern>
          </defs>
          <rect className="plan-surface" width={site.bounds.width} height={site.bounds.height} />
          {site.zones.map((zone) => {
            const left = Math.min(...zone.polygon.map((point) => point.x));
            const bottom = Math.min(...zone.polygon.map((point) => point.y));
            const x = left + 0.9;
            const y = site.bounds.height - bottom - 0.9;
            return (
              <g key={zone.id} className="plan-zone" data-kind={zone.kind}>
                <polygon points={coordinates(zone.polygon, site)} />
                {zone.kind !== "floor" && zone.kind !== "aisle" ? (
                  <text x={x} y={y}>
                    {zone.name}
                  </text>
                ) : null}
              </g>
            );
          })}
          {selectedRoute ? (
            <path className="selected-route" d={routePath(selectedRoute.points, site)} />
          ) : null}
          {trail.length > 1 ? <path className="selected-trail" d={routePath(trail, site)} /> : null}
          {located.map((robot) => {
            if (robot.id !== selectedRobotId && robot.localizationState !== "degraded") return null;
            const ellipse = uncertainty(robot.pose);
            return (
              <ellipse
                key={robot.id}
                className="uncertainty-ring"
                data-status={robot.localizationState}
                cx={robot.pose.x}
                cy={site.bounds.height - robot.pose.y}
                rx={Math.max(ellipse.major, 0.5)}
                ry={Math.max(ellipse.minor, 0.5)}
                transform={`rotate(${ellipse.angle} ${robot.pose.x} ${site.bounds.height - robot.pose.y})`}
              />
            );
          })}
        </svg>
        <div className="robot-markers" aria-label="Robot positions">
          {located.map((robot) => (
            <button
              key={robot.id}
              type="button"
              className="robot-marker"
              data-status={robot.status}
              data-stale={String(robot.stale)}
              aria-label={`${robot.id}, ${robot.status}${robot.stale ? ", stale" : ""}`}
              aria-pressed={robot.id === selectedRobotId}
              style={{
                left: `${(robot.pose.x / site.bounds.width) * 100}%`,
                top: `${((site.bounds.height - robot.pose.y) / site.bounds.height) * 100}%`,
              }}
              onClick={() => onSelectRobot(robot.id)}
            >
              <i style={{ transform: `rotate(${-robot.pose.headingDegrees}deg)` }} aria-hidden="true" />
              <span>{shortId(robot.id)}</span>
            </button>
          ))}
        </div>
      </div>
    </section>
  );
}
