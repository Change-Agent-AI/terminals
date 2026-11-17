"""Backwards-compat aliases for the pre-templates "policies" API.

Change Agent's terminal-server integration (routers/configs.py) detects
orchestrators via ``GET /api/v1/policies`` and saves per-connection policy
data with ``PUT /api/v1/policies/{policy_id}``.  Upstream renamed the
resource to "templates" (``GET/POST /api/v1/templates``,
``PATCH /api/v1/templates/{id}``), which would 404 every policy save from
existing Change Agent deployments.

These shims keep the old routes working against the templates store:

* ``GET    /api/v1/policies``           → list templates
* ``POST   /api/v1/policies``           → create template
* ``GET    /api/v1/policies/{id}``      → read template
* ``PUT    /api/v1/policies/{id}``      → upsert template (create or merge)
* ``PATCH  /api/v1/policies/{id}``      → merge template

The PUT upsert matters: Change Agent creates connections with a policy id
that may not exist yet, and upstream's PATCH returns 404 in that case.
"""

from fastapi import APIRouter, Depends, HTTPException

from terminals.db.session import async_session
from terminals.routers.auth import verify_admin_api_key
from terminals.routers.templates import (
    TemplateCreate,
    TemplateData,
    create_template,
    get_template,
    list_templates,
    update_template,
)
from terminals.utils.template_specs import TemplateNotFoundError

router = APIRouter(
    prefix="/api/v1/policies",
    tags=["policies"],
    dependencies=[Depends(verify_admin_api_key)],
)


@router.get("")
async def list_policies():
    return await list_templates()


@router.post("", status_code=201)
async def create_policy(body: TemplateCreate):
    return await create_template(body)


@router.get("/{policy_id}")
async def get_policy(policy_id: str):
    return await get_template(policy_id)


@router.patch("/{policy_id}")
async def patch_policy(policy_id: str, body: TemplateData):
    return await update_template(policy_id, body)


@router.put("/{policy_id}")
async def put_policy(policy_id: str, body: TemplateData):
    try:
        return await update_template(policy_id, body)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    # Not found yet — create it so the editor's first save succeeds.
    return await create_template(TemplateCreate(id=policy_id, data=body))


__all__ = ["router", "TemplateNotFoundError"]
