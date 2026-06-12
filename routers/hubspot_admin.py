"""
routers/hubspot_admin.py — One-shot setup endpoints.

These are guarded by the same APP_PASSWORD as the rest of the app and are
meant to be hit once per HubSpot account / schema change.

    POST /api/hubspot-admin/bootstrap-properties
        Idempotently creates the Denali BD Automation property group + all 16
        custom properties (9 Deal + 7 Contact). Safe to call repeatedly.

    GET  /api/hubspot-admin/pipeline-check
        Returns the resolved pipeline ID + stage IDs so you can verify the
        stages match what services/hubspot.py expects.
"""

from fastapi import APIRouter, HTTPException

from services import hubspot as hs

router = APIRouter()


@router.post("/bootstrap-properties")
def bootstrap_properties():
    """Create the schema in HubSpot if missing. Idempotent."""
    try:
        result = hs.bootstrap_properties()
    except hs.HubSpotError as e:
        raise HTTPException(500, str(e))
    created = sum(1 for o in result["deals"] + result["contacts"] if o["status"] == "created")
    return {
        "ok":           True,
        "created":      created,
        "already_set":  len(result["deals"]) + len(result["contacts"]) - created,
        "detail":       result,
    }


@router.get("/pipeline-check")
def pipeline_check():
    """Verify the Clinical Trial Outreach pipeline + 7 stages exist."""
    try:
        pid = hs.get_pipeline_id()
        stages = {label: hs.get_stage_id(label) for label in hs.PIPELINE_STAGES}
    except hs.HubSpotError as e:
        raise HTTPException(500, str(e))
    return {"pipeline_id": pid, "stages": stages}
