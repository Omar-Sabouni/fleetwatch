from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from app.main import create_app


async def _with_client(db_path: Path, exercise: Any) -> None:
    application = create_app(db_path)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            await exercise(client)


def test_seeded_snapshot_supports_the_operator_flow(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("FLEETWATCH_PROFILE", "interactive")
    monkeypatch.setenv("FLEETWATCH_CLOCK_MODE", "virtual")

    async def exercise(client: httpx.AsyncClient) -> None:
        site_response = await client.get("/api/site")
        assert site_response.status_code == 200
        site = site_response.json()
        assert site["site_id"] == "dxb-01"
        assert site["deployment"] == {"read_only": False}

        dashboard_response = await client.get("/api/dashboard")
        assert dashboard_response.status_code == 200
        assert dashboard_response.headers["etag"]
        dashboard = dashboard_response.json()
        assert len(dashboard["robots"]) == 8
        assert dashboard["summary"]["total"] == 8
        assert 0 < dashboard["replay"]["current"] < dashboard["replay"]["total"]
        assert dashboard["replay"]["state"] == "paused"

        selected = next(robot for robot in dashboard["robots"] if robot["active_alerts"])
        history_response = await client.get(
            f"/api/robots/{selected['robot_id']}/history",
            params={"limit": 80},
        )
        assert history_response.status_code == 200
        history = history_response.json()
        assert history["robot_id"] == selected["robot_id"]
        assert history["count"] > 0
        timestamps = [event["timestamp"] for event in history["events"]]
        assert timestamps == sorted(timestamps)

    asyncio.run(_with_client(tmp_path / "fleetwatch.db", exercise))


def test_operator_mutations_require_live_entity_revisions(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("FLEETWATCH_PROFILE", "interactive")
    monkeypatch.setenv("FLEETWATCH_CLOCK_MODE", "virtual")

    async def exercise(client: httpx.AsyncClient) -> None:
        dashboard = (await client.get("/api/dashboard")).json()
        incident = next(alert for alert in dashboard["alerts"] if alert["status"] == "opened")
        acknowledge_path = f"/api/alerts/{incident['id']}/acknowledge"

        missing_revision = await client.post(acknowledge_path)
        assert missing_revision.status_code == 428

        acknowledged = await client.post(
            acknowledge_path,
            headers={"If-Match": f'"{incident["version"]}"'},
        )
        assert acknowledged.status_code == 200
        assert acknowledged.json()["status"] == "acknowledged"
        assert acknowledged.json()["version"] == incident["version"] + 1

        stale_acknowledgement = await client.post(
            acknowledge_path,
            headers={"If-Match": f'"{incident["version"]}"'},
        )
        assert stale_acknowledgement.status_code == 409

        replay = dashboard["replay"]
        stepped = await client.post(
            "/api/replay/step",
            headers={"If-Match": f'"{replay["revision"]}"'},
        )
        assert stepped.status_code == 200
        assert stepped.json()["current"] == replay["current"] + 1
        assert stepped.json()["revision"] > replay["revision"]

        stale_step = await client.post(
            "/api/replay/step",
            headers={"If-Match": f'"{replay["revision"]}"'},
        )
        assert stale_step.status_code == 409

    asyncio.run(_with_client(tmp_path / "fleetwatch.db", exercise))
