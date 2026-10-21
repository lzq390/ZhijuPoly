"""Read-only, owner-derived pool balances, durable operation receipts and results."""
from uuid import UUID
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from app.auth.context import current_owner_id
from app.services.entitlement_service import service_from_request, public_entitlement_error
from task_usage import entitlements as core
from task_usage.entitlement_policy import public_policy

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])
_FIELDS = ("operation_id","module","native_task_id","policy_version","pool_id","spec_id","spec",
    "period_start","period_end","generation","state","outcome","qualified",
    "created_at","updated_at","settled_at")

def _ready(request):
    current_owner_id()
    if not core.enabled():
        raise HTTPException(503, detail={"code":"usage_entitlements_disabled",
                                        "message":"Success allowances have not been enabled."})
    return service_from_request(request)

def _history_repository(request):
    current_owner_id()
    repository = getattr(request.app.state, "entitlement_repository", None)
    if repository is None:
        raise HTTPException(503, detail={"code":"usage_entitlements_unavailable"})
    return repository

def _history_call(repository, method, **kwargs):
    try:
        return getattr(repository, method)(owner_user_id=current_owner_id(), **kwargs)
    except core.EntitlementError as exc:
        raise public_entitlement_error(exc) from None
    except Exception:
        raise HTTPException(503, detail={"code":"usage_history_unavailable"}) from None

def _public(operation):
    return jsonable_encoder({name:operation.get(name) for name in _FIELDS})

def _operation(repository, operation_id):
    operation = _history_call(repository, "read_saved_operation", operation_id=str(operation_id))
    if operation is None:
        raise HTTPException(404, detail={"code":"usage_operation_not_found"})
    return operation

@router.get("/entitlements")
def entitlements(request: Request):
    owner = current_owner_id()
    if not core.enabled():
        return {"enabled":False,"mode":"not_enabled","pools":[],
                "policy":None,"message":"No commercial allowance policy is active."}
    repository, policy = _ready(request)
    try:
        result = repository.summary(owner_user_id=owner, policy_version=policy["version"])
        counts = repository.list_request_counts(owner_user_id=owner,
            start=result["period_start"],end=result["period_end"])
    except core.EntitlementError as exc:
        raise public_entitlement_error(exc) from None
    result["request_observations"] = counts
    result["policy"] = public_policy(policy)
    result["mode"] = "example" if policy["example"] else "configured"
    result["period_basis"] = "admission_reservation"
    result["diagnostics_only"] = False
    result["resource_measurements_are_diagnostic"] = True
    result["request_observation_degraded"] = bool(getattr(request.app.state,"entitlement_observation_degraded",False))
    return jsonable_encoder(result)

@router.get("/operations")
def operations(request: Request, module: str | None = Query(None,max_length=64),
               limit: int = Query(25,ge=1,le=100),offset: int = Query(0,ge=0,le=100000)):
    repository = _history_repository(request)
    values = _history_call(repository, "read_saved_operations",module=module,limit=limit+1,offset=offset)
    return {"items":[_public(item) for item in values[:limit]],
            "offset":offset,"limit":limit,"has_more":len(values)>limit}

@router.get("/operations/{operation_id}")
def operation(request: Request, operation_id: UUID):
    repository = _history_repository(request)
    return _public(_operation(repository,operation_id))

@router.get("/operations/{operation_id}/result")
def operation_result(request: Request, operation_id: UUID):
    repository = _history_repository(request)
    value = _operation(repository,operation_id)
    if value["state"] not in core.TERMINAL_STATES:
        raise HTTPException(409,detail={"code":"usage_outcome_unknown" if value["state"]=="unknown" else "usage_operation_pending",
            "operation_id":str(value["operation_id"]),"state":value["state"],"reservation_held":True})
    if value.get("result_json") is None and value.get("result_ref") is None:
        raise HTTPException(410,detail={"code":"usage_result_unavailable",
            "message":"The operation receipt is retained; no recoverable result is available."})
    return jsonable_encoder({"operation":_public(value),"result":value.get("result_json"),"result_ref":value.get("result_ref")})
