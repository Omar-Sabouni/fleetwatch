"""FleetWatch Mission Control FastAPI entrypoint and application factory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import mimetypes
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path as FilePath
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .async_utils import finish_before_cancellation
from .async_utils import run_sync as run_in_threadpool
from .replay import ReplayController, ReplayOperationError, ReplayRevisionConflict
from .schemas import (
    SQLITE_INTEGER_MAX,
    AlertResponse,
    DashboardResponse,
    HealthResponse,
    HistoryResponse,
    IngestResponse,
    ReplayResponse,
    ReplayStart,
    RobotIdentifier,
    SiteResponse,
    TelemetryBatch,
    TelemetryEvent,
)
from .site import SiteConfiguration
from .store import (
    EventIDConflict,
    FleetStore,
    FutureTimestampError,
    SourceSequenceConflict,
    load_events_jsonl,
)

LOGGER = logging.getLogger(__name__)
APP_DIR = FilePath(__file__).resolve().parent
DEFAULT_TELEMETRY_PATH = APP_DIR / "data" / "telemetry.jsonl"
DEFAULT_SITE_PATH = APP_DIR / "data" / "site.json"
MAX_TELEMETRY_BODY_BYTES = 256 * 1024
IDENTIFIER_PATTERN = r"^[A-Za-z0-9._:-]+$"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
PUBLIC_DEMO_PROFILE = "public-demo"
@dataclass(frozen=True)
class Services:
    store: FleetStore
    replay: ReplayController
    site: SiteConfiguration


class TelemetryBodyLimitMiddleware:
    """Count streamed ASGI request chunks before handing JSON to FastAPI."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/api/telemetry":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared:
            try:
                if int(declared) > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                pass
        messages: list[Message] = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
        index = 0

        async def replay_receive() -> Message:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": f"telemetry request body exceeds {self.max_bytes} bytes"},
        )
        await response(scope, receive, send)


class SameOriginMutationMiddleware:
    """Reject unsafe drive-by browser requests without blocking device clients."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        origin = headers.get("origin")
        fetch_site = headers.get("sec-fetch-site")
        if origin is None and fetch_site is None:
            await self.app(scope, receive, send)
            return
        if origin is None or origin.lower() == "null":
            await self._reject(scope, receive, send)
            return
        try:
            parsed = urlsplit(origin)
            malformed = (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or bool(parsed.query)
                or bool(parsed.fragment)
            )
        except ValueError:
            malformed = True
        request_host = headers.get("host", "")
        expected_origin = f"{scope.get('scheme', 'http')}://{request_host}".lower()
        if (
            malformed
            or origin.rstrip("/").lower() != expected_origin.rstrip("/")
            or (fetch_site is not None and fetch_site.lower() != "same-origin")
        ):
            await self._reject(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=403,
            content={"detail": "cross-site mutation request rejected"},
        )
        await response(scope, receive, send)


class SecurityHeadersMiddleware:
    """Apply a compact browser hardening policy to every HTTP response."""

    _headers = (
        (
            b"content-security-policy",
            b"default-src 'self'; script-src 'self'; style-src 'self'; "
            b"style-src-elem 'self'; style-src-attr 'unsafe-inline'; "
            b"img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            b"base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        ),
        (b"x-frame-options", b"DENY"),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
    )

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {key.lower() for key, _ in headers}
                headers.extend((key, value) for key, value in self._headers if key not in existing)
                if scope.get("path", "").startswith("/assets/"):
                    headers = [
                        (key, value) for key, value in headers if key.lower() != b"cache-control"
                    ]
                    headers.append((b"cache-control", b"public, max-age=31536000, immutable"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _allowed_hosts() -> list[str]:
    configured = os.environ.get("FLEETWATCH_ALLOWED_HOSTS")
    if configured:
        hosts = [host.strip() for host in configured.split(",") if host.strip()]
        if hosts:
            return hosts
    return ["127.0.0.1", "localhost", "testserver"]


def _etag(process_nonce: str, store_revision: int, replay_revision: int) -> str:
    material = f"{process_nonce}:{store_revision}:{replay_revision}".encode()
    return f'"{hashlib.sha256(material).hexdigest()}"'


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate == etag:
            return True
    return False


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _parse_entity_version(header: str | None, *, entity: str, entity_id: int | None = None) -> int:
    """Parse the optimistic-concurrency token required by operator mutations."""

    if header is None:
        raise HTTPException(
            status_code=428,
            detail={"code": "precondition_required", "header": "If-Match"},
        )
    candidate = header.strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:].strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] == '"':
        candidate = candidate[1:-1]
    prefix = f"{entity}-"
    if entity_id is not None:
        prefix += f"{entity_id}-"
    if candidate.startswith(prefix):
        candidate = candidate[len(prefix) :]
    try:
        version = int(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid If-Match revision") from exc
    if version < 0:
        raise HTTPException(status_code=400, detail="invalid If-Match revision")
    return version


def create_app(
    db_path: str | FilePath | None = None,
    telemetry_path: str | FilePath | None = None,
    seed: bool = True,
    *,
    site_path: str | FilePath | None = None,
) -> FastAPI:
    """Build an isolated application without writing during module import."""

    resolved_db = FilePath(
        db_path
        or os.environ.get(
            "FLEETWATCH_DB_PATH", str(FilePath.cwd() / "fleetwatch-mission-control.db")
        )
    )
    resolved_telemetry = FilePath(
        telemetry_path or os.environ.get("FLEETWATCH_TELEMETRY_PATH", str(DEFAULT_TELEMETRY_PATH))
    )
    resolved_site = FilePath(
        site_path or os.environ.get("FLEETWATCH_SITE_PATH", str(DEFAULT_SITE_PATH))
    )
    profile = os.environ.get("FLEETWATCH_PROFILE", "interactive").strip().lower()
    read_only_alias = os.environ.get("FLEETWATCH_READ_ONLY", "").strip().lower()
    if read_only_alias in {"1", "true", "yes", "on"}:
        profile = PUBLIC_DEMO_PROFILE
    if profile not in {"interactive", PUBLIC_DEMO_PROFILE}:
        raise ValueError("FLEETWATCH_PROFILE must be 'interactive' or 'public-demo'")
    clock_mode_raw = os.environ.get("FLEETWATCH_CLOCK_MODE", "virtual").strip().lower()
    if clock_mode_raw not in {"virtual", "wall"}:
        raise ValueError("FLEETWATCH_CLOCK_MODE must be 'virtual' or 'wall'")
    clock_mode: Literal["virtual", "wall"] = "wall" if clock_mode_raw == "wall" else "virtual"
    process_nonce = secrets.token_hex(12)
    initialization_lock = asyncio.Lock()
    static_dir = APP_DIR / "static"

    def scenario_fingerprint(site: SiteConfiguration) -> str:
        """Bind persisted state to the packaged site and telemetry."""

        identity = site.public_payload()
        digest = hashlib.sha256(b"fleetwatch-packaged-scenario-v2\0")
        digest.update(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\0")
        digest.update(resolved_telemetry.read_bytes())
        return digest.hexdigest()

    def build_services() -> Services:
        if str(resolved_db) != ":memory:":
            resolved_db.parent.mkdir(parents=True, exist_ok=True)
        site = SiteConfiguration.load(resolved_site)
        store = FleetStore(str(resolved_db), site=site, clock_mode=clock_mode)
        try:
            scenario = site.public_payload()["scenario"]
            fingerprint = scenario_fingerprint(site)
            persisted_fingerprint = store.metadata_value("packaged_scenario_fingerprint")
            fixture_events = load_events_jsonl(resolved_telemetry)
            telemetry_count = store.telemetry_count()
            exact_fixture_matches = sum(
                store.event_status(event) == "exact" for event in fixture_events
            )
            if (
                profile == PUBLIC_DEMO_PROFILE
                and telemetry_count > 0
                and (
                    persisted_fingerprint != fingerprint or exact_fixture_matches != telemetry_count
                )
            ):
                # A read-only database is explicitly disposable. This repairs
                # upgrades from the old packaged fixture while interactive
                # deployments retain every user-ingested row.
                store.clear()
            if profile == PUBLIC_DEMO_PROFILE:
                store.set_metadata_value("packaged_scenario_fingerprint", fingerprint)
            replay = ReplayController(
                store,
                resolved_telemetry,
                default_ticks_per_second=float(scenario.get("default_ticks_per_second", 2.0)),
                loop=profile == PUBLIC_DEMO_PROFILE,
            )
            if len(replay.ticks) != int(scenario["total_ticks"]):
                raise ValueError(
                    "telemetry fixture tick count does not match site scenario metadata"
                )
            if seed and store.telemetry_count() == 0:
                replay.seed_to_checkpoint(int(scenario.get("initial_tick", 0)))
                store.set_metadata_value("packaged_scenario_fingerprint", fingerprint)
            return Services(store=store, replay=replay, site=site)
        except Exception:
            store.close()
            raise

    async def liveness_loop(store: FleetStore) -> None:
        while True:
            await asyncio.sleep(5.0)
            try:
                await run_in_threadpool(store.evaluate_liveness)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("wall-clock liveness evaluator failed")

    async def ensure_initialized() -> Services:
        current: Services | None = getattr(application.state, "services", None)
        if current is not None:
            return current
        async with initialization_lock:
            current = getattr(application.state, "services", None)
            if current is not None:
                return current

            async def construct_boot_and_publish() -> Services:
                service = await run_in_threadpool(build_services)
                liveness_task: asyncio.Task[None] | None = None
                try:
                    if profile == PUBLIC_DEMO_PROFILE:
                        replay_status = service.replay.status()
                        if replay_status["state"] == "complete":
                            await service.replay.restart(prime_first_tick=True)
                        else:
                            await service.replay.start()
                    if clock_mode == "wall":
                        liveness_task = asyncio.create_task(
                            liveness_loop(service.store), name="fleetwatch-liveness"
                        )
                except BaseException:

                    async def discard_service() -> None:
                        try:
                            if liveness_task is not None:
                                liveness_task.cancel()
                                with suppress(asyncio.CancelledError):
                                    await liveness_task
                        finally:
                            try:
                                await service.replay.shutdown()
                            finally:
                                await run_in_threadpool(service.store.close)

                    await finish_before_cancellation(discard_service())
                    raise

                application.state.services = service
                application.state.store = service.store
                application.state.replay = service.replay
                application.state.site = service.site
                application.state.liveness_task = liveness_task
                return service

            return await finish_before_cancellation(construct_boot_and_publish())

    async def teardown_services(services: Services) -> None:
        """Release every service resource even when shutdown is cancelled."""

        async def teardown() -> None:
            liveness_task: asyncio.Task[None] | None = application.state.liveness_task
            try:
                if liveness_task is not None:
                    liveness_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await liveness_task
            finally:
                try:
                    await services.replay.shutdown()
                finally:
                    try:
                        await run_in_threadpool(services.store.close)
                    finally:
                        application.state.services = None
                        application.state.store = None
                        application.state.replay = None
                        application.state.site = None
                        application.state.liveness_task = None

        await finish_before_cancellation(teardown())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if not (static_dir / "index.html").is_file():
            raise RuntimeError(
                "FleetWatch frontend assets are missing; build frontend/ before startup"
            )
        services: Services | None = None
        try:
            services = await ensure_initialized()
            yield
        finally:
            initialized = services or getattr(application.state, "services", None)
            if initialized is not None:
                await teardown_services(initialized)

    application = FastAPI(
        title="FleetWatch Mission Control",
        version="current",
        description="A warehouse fleet operations and incident console",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    application.state.services = None
    application.state.store = None
    application.state.replay = None
    application.state.site = None
    application.state.liveness_task = None
    application.state.profile = profile
    application.state.read_only = profile == PUBLIC_DEMO_PROFILE

    @application.exception_handler(RequestValidationError)
    async def request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": _json_safe(exc.errors())})

    async def services_dependency() -> Services:
        try:
            return await ensure_initialized()
        except Exception as exc:
            LOGGER.exception("FleetWatch service initialization failed")
            raise HTTPException(status_code=503, detail="service unavailable") from exc

    async def mutable_profile() -> None:
        if profile == PUBLIC_DEMO_PROFILE:
            raise HTTPException(
                status_code=403, detail="mutations are disabled in read-only mode"
            )

    application.add_middleware(
        GZipMiddleware,
        minimum_size=500,
    )
    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=_allowed_hosts(), www_redirect=False
    )
    application.add_middleware(SameOriginMutationMiddleware)
    application.add_middleware(TelemetryBodyLimitMiddleware, max_bytes=MAX_TELEMETRY_BODY_BYTES)
    application.add_middleware(SecurityHeadersMiddleware)
    @application.get("/livez", response_model=HealthResponse)
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    async def readiness(services: Services) -> dict[str, str]:
        try:
            await run_in_threadpool(services.store.probe)
            services.site.public_payload()
        except Exception as exc:
            LOGGER.warning("FleetWatch readiness probe failed: %s", exc)
            raise HTTPException(status_code=503, detail="storage unavailable") from exc
        return {"status": "ok"}

    @application.get("/readyz", response_model=HealthResponse)
    async def readyz(
        services: Services = Depends(services_dependency),
    ) -> dict[str, str]:
        return await readiness(services)

    @application.get("/api/site", response_model=SiteResponse)
    async def site_metadata(
        request: Request,
        services: Services = Depends(services_dependency),
    ) -> Response:
        payload = services.site.public_payload()
        payload["deployment"] = {
            "read_only": profile == PUBLIC_DEMO_PROFILE,
        }
        material = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        etag = f'"{hashlib.sha256(material).hexdigest()}"'
        if _etag_matches(request.headers.get("if-none-match"), etag):
            return Response(
                status_code=304,
                headers={"ETag": etag, "Cache-Control": "public, max-age=3600"},
            )
        return JSONResponse(
            payload,
            headers={"ETag": etag, "Cache-Control": "public, max-age=3600"},
        )

    @application.post("/api/telemetry", response_model=IngestResponse)
    async def ingest_telemetry(
        payload: TelemetryEvent | TelemetryBatch,
        _: None = Depends(mutable_profile),
        services: Services = Depends(services_dependency),
    ) -> dict[str, object]:
        events = payload.events if isinstance(payload, TelemetryBatch) else [payload]
        try:
            return await run_in_threadpool(services.store.ingest_events, events)
        except EventIDConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "event_id_conflict",
                    "event_id": exc.event_id,
                    "message": str(exc),
                },
            ) from exc
        except SourceSequenceConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "telemetry_sequence_conflict",
                    "message": "telemetry sequence is already in use",
                },
            ) from exc
        except FutureTimestampError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "future_timestamp",
                    "event_id": exc.event_id,
                    "max_future_skew_seconds": 300,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.get("/api/robots/{robot_id}/history", response_model=HistoryResponse)
    async def robot_history(
        robot_id: Annotated[
            RobotIdentifier,
            Path(min_length=1, max_length=100, pattern=IDENTIFIER_PATTERN),
        ],
        services: Services = Depends(services_dependency),
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> dict[str, object]:
        payload = await run_in_threadpool(services.store.robot_history_payload, robot_id, limit)
        if payload["count"] == 0:
            raise HTTPException(status_code=404, detail="robot not found")
        return payload

    @application.post("/api/alerts/{alert_id}/acknowledge", response_model=AlertResponse)
    async def acknowledge_alert(
        alert_id: Annotated[int, Path(ge=1, le=SQLITE_INTEGER_MAX)],
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
        _: None = Depends(mutable_profile),
        services: Services = Depends(services_dependency),
    ) -> dict[str, object]:
        expected = _parse_entity_version(if_match, entity="alert", entity_id=alert_id)
        result, alert = await run_in_threadpool(
            services.store.acknowledge_alert, alert_id, expected
        )
        if result == "missing":
            raise HTTPException(status_code=404, detail="alert not found")
        if result == "conflict":
            assert alert is not None
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "alert_revision_conflict",
                    "current_version": alert["version"],
                },
            )
        if result == "recovered":
            raise HTTPException(status_code=409, detail="recovered alerts cannot be acknowledged")
        assert alert is not None
        response.headers["ETag"] = f'"alert-{alert_id}-{alert["version"]}"'
        return alert

    def replay_representation_etag(replay: dict[str, Any]) -> str:
        material = json.dumps(
            _json_safe(replay),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return f'"{hashlib.sha256(material).hexdigest()}"'

    def set_replay_headers(response: Response, replay: dict[str, Any]) -> None:
        response.headers["ETag"] = replay_representation_etag(replay)
        response.headers["X-FleetWatch-Control-Revision"] = str(replay["revision"])
        response.headers["Cache-Control"] = "no-cache"

    async def run_replay_mutation(action: Any) -> dict[str, Any]:
        try:
            return await action
        except ReplayRevisionConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "replay_revision_conflict",
                    "current_revision": exc.current_revision,
                },
            ) from exc
        except ReplayOperationError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "replay_error", "message": str(exc)},
            ) from exc

    @application.post("/api/replay/start", response_model=ReplayResponse)
    async def replay_start(
        response: Response,
        payload: ReplayStart | None = None,
        if_match: Annotated[str | None, Header()] = None,
        _: None = Depends(mutable_profile),
        services: Services = Depends(services_dependency),
    ) -> dict[str, object]:
        expected = _parse_entity_version(if_match, entity="replay")
        rate = payload.ticks_per_second if payload else None
        replay = await run_replay_mutation(services.replay.start(rate, expected_revision=expected))
        set_replay_headers(response, replay)
        return replay

    @application.post("/api/replay/pause", response_model=ReplayResponse)
    async def replay_pause(
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
        _: None = Depends(mutable_profile),
        services: Services = Depends(services_dependency),
    ) -> dict[str, object]:
        expected = _parse_entity_version(if_match, entity="replay")
        replay = await run_replay_mutation(services.replay.pause(expected_revision=expected))
        set_replay_headers(response, replay)
        return replay

    @application.post("/api/replay/step", response_model=ReplayResponse)
    async def replay_step(
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
        _: None = Depends(mutable_profile),
        services: Services = Depends(services_dependency),
    ) -> dict[str, object]:
        expected = _parse_entity_version(if_match, entity="replay")
        replay = await run_replay_mutation(services.replay.step(expected_revision=expected))
        set_replay_headers(response, replay)
        return replay

    @application.post("/api/replay/restart", response_model=ReplayResponse)
    async def replay_restart(
        response: Response,
        payload: ReplayStart | None = None,
        if_match: Annotated[str | None, Header()] = None,
        _: None = Depends(mutable_profile),
        services: Services = Depends(services_dependency),
    ) -> dict[str, object]:
        expected = _parse_entity_version(if_match, entity="replay")
        rate = payload.ticks_per_second if payload else None
        replay = await run_replay_mutation(
            services.replay.restart(rate, expected_revision=expected)
        )
        set_replay_headers(response, replay)
        return replay

    async def build_dashboard_payload(
        services: Services,
    ) -> tuple[dict[str, Any], str]:
        snapshot, replay = await services.replay.dashboard_snapshot()
        store_revision = int(snapshot.pop("revision"))
        replay_revision = int(replay["revision"])
        fleet_time = snapshot.pop("as_of")
        revision = f"{process_nonce}:{store_revision}:{replay_revision}"
        payload = DashboardResponse.model_validate(
            {
                "revision": revision,
                "fleet_time": fleet_time,
                **snapshot,
                "replay": replay,
            }
        ).model_dump(mode="json")
        return payload, _etag(process_nonce, store_revision, replay_revision)

    @application.get("/api/dashboard", response_model=DashboardResponse)
    async def dashboard_snapshot(
        request: Request,
        services: Services = Depends(services_dependency),
    ) -> Any:
        store_revision, replay_revision = await services.replay.dashboard_revisions()
        current_etag = _etag(process_nonce, store_revision, replay_revision)
        if _etag_matches(request.headers.get("if-none-match"), current_etag):
            return Response(
                status_code=304,
                headers={"ETag": current_etag, "Cache-Control": "no-cache"},
            )
        payload, current_etag = await build_dashboard_payload(services)
        return JSONResponse(
            content=payload,
            headers={"ETag": current_etag, "Cache-Control": "no-cache"},
        )

    @application.get("/assets/{asset_path:path}", include_in_schema=False)
    async def dashboard_asset(asset_path: str) -> Response:
        asset_root = (static_dir / "assets").resolve()
        candidate = (asset_root / asset_path).resolve()
        try:
            candidate.relative_to(asset_root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="asset not found") from exc
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="asset not found")
        content = await run_in_threadpool(candidate.read_bytes)
        media_type, _ = mimetypes.guess_type(candidate.name)
        return Response(content, media_type=media_type or "application/octet-stream")

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard_page() -> HTMLResponse:
        index_path = static_dir / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=503, detail="frontend assets are missing")
        index_html = await run_in_threadpool(index_path.read_text, encoding="utf-8")
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})

    return application


app = create_app()
