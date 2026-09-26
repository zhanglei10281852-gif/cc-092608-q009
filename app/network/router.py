from __future__ import annotations

from fastapi import APIRouter, Query

from app.database import get_connection
from app.network.analytics import NetworkAnalytics, ReportWindow
from app.network.schemas import AccelerationStart, ApplicationCreate, BatchSamples, EntitlementCreate, ExperienceSampleCreate, PolicyCreate, PolicyPublish, ScenarioCreate, SegmentCreate, SessionFinish
from app.network.service import NetworkAccelerationService

router = APIRouter(prefix="/api/network", tags=["5G-A 场景加速"])


def service() -> NetworkAccelerationService:
    return NetworkAccelerationService()


@router.post("/scenarios", status_code=201)
def create_scenario(payload: ScenarioCreate):
    return service().create_scenario(payload.model_dump())


@router.get("/scenarios")
def list_scenarios(status: str | None = None):
    return {"items": service().list_scenarios(status)}


@router.post("/scenarios/{scenario_code}/segments", status_code=201)
def add_segment(scenario_code: str, payload: SegmentCreate):
    return service().add_segment(scenario_code, payload.model_dump())


@router.post("/applications", status_code=201)
def create_application(payload: ApplicationCreate):
    return service().create_application(payload.model_dump())


@router.get("/applications")
def list_applications(category: str | None = None):
    return {"items": service().list_applications(category)}


@router.post("/scenarios/{scenario_code}/policies", status_code=201)
def create_policy(scenario_code: str, payload: PolicyCreate):
    return service().create_policy(scenario_code, payload.rules, payload.actor)


@router.post("/policies/{policy_id}/publish")
def publish_policy(policy_id: int, payload: PolicyPublish):
    return service().publish_policy(policy_id, payload.actor, payload.effective_from)


@router.post("/entitlements", status_code=201)
def add_entitlement(payload: EntitlementCreate):
    return service().add_entitlement(payload.model_dump())


@router.post("/samples", status_code=202)
def ingest_sample(payload: ExperienceSampleCreate):
    return service().ingest_sample(payload.model_dump())


@router.post("/samples/batch", status_code=202)
def ingest_batch(payload: BatchSamples):
    return service().ingest_batch([item.model_dump() for item in payload.items])


@router.get("/incidents")
def open_incidents(scenario_code: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().open_incidents(scenario_code, limit)}


@router.post("/incidents/{incident_id}/accelerate")
def start_acceleration(incident_id: int, payload: AccelerationStart):
    return service().start_acceleration(incident_id, payload.actor)


@router.get("/sessions/{session_id}")
def get_session(session_id: int):
    return service().get_session(session_id)


@router.post("/sessions/{session_id}/finish")
def finish_session(session_id: int, payload: SessionFinish):
    return service().finish_session(session_id, payload.actor, payload.reason, payload.result)


@router.post("/sessions/expire")
def expire_sessions(actor: str = Query(default="session-reaper", min_length=1)):
    return service().expire_sessions(actor)


@router.get("/summary")
def summary():
    return service().summary()


@router.get("/analytics/quality")
def quality_report(started_at: str | None = None, ended_at: str | None = None):
    analytics = NetworkAnalytics(get_connection())
    window = ReportWindow(started_at, ended_at)
    return {
        "overview": analytics.quality_overview(window),
        "scenarios": analytics.scenario_breakdown(window),
        "applications": analytics.application_breakdown(window),
        "segments": analytics.segment_breakdown(None, window),
        "severity": analytics.severity_distribution(None, window),
    }


@router.get("/analytics/capacity")
def capacity_report():
    return {"items": NetworkAnalytics(get_connection()).capacity_snapshot()}


@router.get("/analytics/outcomes")
def outcome_report(started_at: str | None = None, ended_at: str | None = None):
    return NetworkAnalytics(get_connection()).acceleration_outcomes(ReportWindow(started_at, ended_at))


@router.get("/events")
def event_timeline(after_id: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
    return NetworkAnalytics(get_connection()).event_timeline(after_id=after_id, limit=limit)


@router.get("/analytics/segments")
def segment_report(scenario_code: str | None = None, started_at: str | None = None, ended_at: str | None = None):
    return {"items": NetworkAnalytics(get_connection()).segment_breakdown(scenario_code, ReportWindow(started_at, ended_at))}


@router.get("/analytics/stale-incidents")
def stale_incidents(before: str, limit: int = Query(default=100, ge=1, le=500)):
    return {"items": NetworkAnalytics(get_connection()).stale_open_incidents(before, limit=limit)}


@router.post("/demo/seed")
def seed_demo():
    return service().seed_demo()
