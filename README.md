# FleetWatch

FleetWatch is a compact operations board for an eight-robot warehouse fleet. It answers three questions: which unit needs attention, where is it, and what changed?

The DXB-01 shift includes low battery, overheating, stale telemetry, poor localization, and restricted-zone incidents. The app opens with an active fleet state instead of an empty dashboard.

## What is here

- A flat fleet ledger sorted by urgency.
- A facility plan with live robot positions, the selected route, recent movement, and localization uncertainty.
- One selected-unit pane with alarms, acknowledgement, current readings, policy limits, and three short trends.
- Compact shift controls for the active operation.
- FastAPI, SQLite, React, and a production frontend.

## Run

Requires Python 3.11 or newer:

```bash
./start.sh
```

Open <http://127.0.0.1:8000>. The launcher creates `.venv` and installs the Python package on first use.

To change the frontend:

```bash
npm install
npm run dev
```

The Vite server runs at <http://127.0.0.1:5173> and proxies the API to the Python server.

## Docker

```bash
docker build -t fleetwatch .
docker run --rm -p 127.0.0.1:8000:8000 -v fleetwatch-data:/data fleetwatch
```

## Main API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/site` | Facility geometry and policy limits |
| `GET` | `/api/dashboard` | Current fleet, incidents, and shift state |
| `GET` | `/api/robots/{id}/history` | Recent telemetry for one unit |
| `POST` | `/api/alerts/{id}/acknowledge` | Acknowledge an incident |
| `POST` | `/api/replay/{start,pause,step,restart}` | Control the active shift |
| `POST` | `/api/telemetry` | Ingest telemetry |

Mutations use the current entity revision in `If-Match`. Telemetry and incidents persist in SQLite.

## Project shape

```text
frontend/src/     React board and API adapter
app/              FastAPI service, shift controller, policy engine, and SQLite store
app/data/         DXB-01 geometry and telemetry
app/static/       Vite production output served by FastAPI
```

MIT licensed.
