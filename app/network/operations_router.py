from __future__ import annotations

from fastapi import APIRouter, Query

from app.network.operations import NetworkOperationsService
from app.network.operations_schemas import CampaignAction, MaintenanceCreate, MaintenanceOverride, RolloutCampaignCreate

router = APIRouter(prefix="/api/network/operations", tags=["网络发布与维护"])


def service() -> NetworkOperationsService:
    return NetworkOperationsService()


@router.post("/campaigns", status_code=201)
def create_campaign(payload: RolloutCampaignCreate):
    return service().create_campaign(payload.model_dump())


@router.get("/campaigns")
def list_campaigns(scenario_code: str | None = None, state: str | None = None):
    return {"items": service().list_campaigns(scenario_code, state)}


@router.get("/campaigns/{campaign_id}")
def campaign_detail(campaign_id: int):
    return service().campaign_detail(campaign_id)


@router.post("/campaigns/{campaign_id}/start")
def start_campaign(campaign_id: int, payload: CampaignAction):
    return service().start_campaign(campaign_id, payload.actor, payload.reason)


@router.post("/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: int, payload: CampaignAction):
    return service().pause_campaign(campaign_id, payload.actor, payload.reason)


@router.post("/campaigns/{campaign_id}/complete")
def complete_campaign(campaign_id: int, payload: CampaignAction):
    return service().complete_campaign(campaign_id, payload.actor, payload.reason)


@router.post("/maintenance", status_code=201)
def create_maintenance(payload: MaintenanceCreate):
    return service().create_maintenance(payload.model_dump())


@router.get("/maintenance/{window_id}")
def maintenance_detail(window_id: int):
    return service().maintenance_detail(window_id)


@router.get("/maintenance/{window_id}/drain")
def maintenance_drain(window_id: int):
    return service().drain_status(window_id)


@router.post("/maintenance/{window_id}/sessions/{session_id}/override")
def override_maintenance_session(window_id: int, session_id: int, payload: MaintenanceOverride):
    return service().override_session(window_id, session_id, payload.action, payload.actor, payload.reason)


@router.post("/maintenance/advance")
def advance_maintenance(actor: str = Query(default="maintenance-scheduler", min_length=1)):
    return service().activate_due_maintenance(actor)
