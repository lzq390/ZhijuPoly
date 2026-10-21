"""Durable success receipts for bounded synchronous product operations.

Only typed, server-derived facts decide qualification. No provider calls occur
here, and quota state never changes the native execution/resource guards.
"""
from __future__ import annotations
from functools import wraps, partial
from io import BytesIO
import hashlib
import inspect
import json
from typing import get_type_hints
import math
from uuid import uuid4

import anyio
from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.auth.context import current_owner_id
from app.services.entitlement_service import (
    request_idempotency_key, request_fingerprint, service_from_request,
    public_entitlement_error, operation_header,
)
from task_usage import entitlements as core
from task_usage.entitlement_policy import normalize_spec, qualify


def _valid_smiles(value):
    if not isinstance(value, str) or not value:
        return False
    from rdkit import Chem
    return Chem.MolFromSmiles(value) is not None


def _facts(module, body, result, request=None):
    base = {"validated": True, "completion_proven": True}
    if module == "predict":
        values = result.get("predictions", {})
        expected = set(body["properties"])
        good = isinstance(values, dict) and set(values) == expected and all(
            not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
            for value in values.values())
        return {**base, "validated": good, "returned_properties": len(values) if isinstance(values, dict) else 0}
    if module == "query":
        value = result.get("predicted_property_value")
        rows = result.get("results")
        predicted = int(not isinstance(value, bool) and isinstance(value, (int,float)) and math.isfinite(value))
        valid = (result.get("match_type") == "property" and
                 result.get("predicted_property_name") == body.get("property_name") and
                 isinstance(rows, list) and result.get("total") == len(rows) and len(rows) <= body["top_k"])
        return {**base, "validated":valid, "predicted_properties":predicted,
                "match_count":len(rows) if isinstance(rows,list) else 0,
                "no_match":isinstance(rows,list) and not rows and result.get("total") == 0}
    if module == "online_knowledge":
        facts = getattr(request.state, "online_entitlement_facts", {}) if request else {}
        if not all(type(facts.get(key)) is int and facts[key] >= 0
                   for key in ("successful_extractions", "failed_extractions")):
            return None
        return {"validated":facts.get("validated") is True,
                "completion_proven":facts.get("completion_proven") is True,
                "report_present":bool(result), "example_used":result.get("exampleUsed") is True,
                "successful_extractions":facts["successful_extractions"],
                "failed_extractions":facts["failed_extractions"]}
    if module == "ocsr":
        return {**base, "output_present": _valid_smiles(result.get("smiles"))}
    if module == "structure":
        from rdkit import Chem
        text = result.get("molblock")
        mol = Chem.MolFromMolBlock(text) if isinstance(text, str) and text else None
        present = bool(mol is not None and mol.GetNumAtoms() and mol.GetNumConformers())
        if present:
            coords = mol.GetConformer().GetPositions()
            present = bool(all(math.isfinite(float(x)) for row in coords for x in row))
        return {**base, "output_present": present}
    if module == "retrosynthesis":
        candidates = result.get("candidates", [])
        unique = set()
        for item in candidates:
            reactants = item.get("reactants", [])
            if item.get("valid_smiles") and reactants and all(
                part.get("valid_smiles") and _valid_smiles(part.get("canonical_smiles") or part.get("input_smiles"))
                for part in reactants):
                unique.add(item.get("canonical_reactants_smiles") or item.get("reactants_smiles"))
        return {**base, "valid_count": len(unique), "no_match": not candidates and result.get("total") == 0}
    if module == "polymerization":
        candidates = result.get("results", [])
        valid = {item.get("polymer_smiles") for item in candidates if _valid_smiles(item.get("polymer_smiles"))}
        return {**base, "valid_count": len(valid), "no_match": not candidates and result.get("total") == 0}
    raise ValueError("Unsupported synchronous entitlement module")


async def _request_data(module, arguments, request):
    model = arguments.get("request_body")
    body = model.model_dump(mode="json") if model is not None else {}
    if module == "predict":
        spec = {"property_count": len(set(body["properties"]))}
    elif module == "query":
        spec = {"match_mode":body["match_mode"], "property_count":1,
                "top_k":body["top_k"], "similarity_threshold":body["similarity_threshold"]}
    elif module == "online_knowledge":
        spec = {"mode":body["mode"], "max_papers":body["max_papers"]}
    elif module == "retrosynthesis":
        spec = {key: body[key] for key in ("num_beams", "num_return_sequences", "max_new_tokens")}
    elif module == "polymerization":
        spec = {"max_results": body["max_results"],
                "monomer_count": 1 + int(bool(body.get("monomer_b_smiles")))}
    elif module == "structure":
        from rdkit import Chem
        mol = Chem.MolFromSmiles(body["smiles"])
        if mol is None:
            raise HTTPException(422, "invalid smiles")
        spec = {"operation": "3d", "atom_count": mol.GetNumAtoms()}
    elif module == "ocsr":
        upload = arguments["image"]
        settings = request.app.state.settings
        raw = await upload.read(min(settings.ocsr_max_image_bytes, 10 * 1024**2) + 1)
        await upload.seek(0)
        if len(raw) > min(settings.ocsr_max_image_bytes, 10 * 1024**2):
            raise HTTPException(413, "Image exceeds the input size limit")
        try:
            from PIL import Image
            with Image.open(BytesIO(raw)) as image:
                pixels = image.width * image.height
        except Exception as exc:
            raise HTTPException(422, "Invalid image") from exc
        spec = {"image_bytes": len(raw), "image_pixels": pixels}
        body = {"image_sha256": hashlib.sha256(raw).hexdigest(), "content_type": upload.content_type,
                "image_bytes": len(raw)}
    else:
        raise ValueError("Unsupported synchronous entitlement module")
    return body, spec


def _replay(operation):
    state = operation["state"]
    stored = operation.get("result_json")
    if state in {"succeeded", "released"} and isinstance(stored, dict) and "payload" in stored:
        return JSONResponse(stored["payload"], status_code=stored.get("http_status", 200),
                            headers={**operation_header(operation), "X-Usage-Replayed": "true", "X-Usage-State":state})
    code = "usage_outcome_unknown" if state == "unknown" else "usage_operation_pending"
    raise HTTPException(409, detail={"code": code, "operation_id": str(operation["operation_id"]),
        "state": state, "reservation_held": state not in core.TERMINAL_STATES},
        headers={**operation_header(operation), "X-Usage-State":state})


async def _call(repository, name, **values):
    return await run_in_threadpool(partial(getattr(repository, name), **values))


def entitled_sync(module):
    """Wrap a typed FastAPI endpoint, inside validation and existing authentication."""
    def decorate(function):
        hints = get_type_hints(function)
        original = inspect.signature(function)
        signature = original.replace(parameters=[p.replace(annotation=hints.get(name,p.annotation))
            for name,p in original.parameters.items()], return_annotation=hints.get("return",original.return_annotation))
        async def invoke(*args, **kwargs):
            if inspect.iscoroutinefunction(function):
                return await function(*args, **kwargs)
            return await run_in_threadpool(function, *args, **kwargs)
        @wraps(function)
        async def wrapped(*args, **kwargs):
            if not core.enabled():
                return await invoke(*args, **kwargs)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = bound.arguments
            if module == "query" and arguments["request_body"].match_mode != "property":
                return await invoke(*args, **kwargs)
            request = arguments.get("request")
            if not isinstance(request, Request):
                raise RuntimeError("Entitled synchronous endpoints require Request")
            owner = current_owner_id()
            repository = getattr(request.app.state, "entitlement_repository", None)
            if repository is None:
                raise HTTPException(503, detail={"code": "usage_entitlements_unavailable"})
            key = request_idempotency_key(request)
            operation = None
            execution_started = False
            body = {}
            try:
                body, spec = await _request_data(module, arguments, request)
                fingerprint = request_fingerprint(body)
                existing = await _call(repository, "lookup", owner_user_id=owner, module=module,
                                       idempotency_key=key, request_fingerprint=fingerprint)
                if existing:
                    request.state.usage_operation_id = str(existing["operation_id"])
                    request.state.usage_operation_state = existing["state"]
                    return _replay(existing)
                _, policy = service_from_request(request)
                try:
                    normalize_spec(policy, module, spec)
                except ValueError as exc:
                    raise HTTPException(422, detail={"code": "usage_specification_exceeded",
                                                    "message": "Task exceeds its permitted specification."}) from exc
                reserved = await _call(repository, "reserve", owner_user_id=owner, module=module,
                    idempotency_key=key, request_fingerprint=fingerprint,
                    policy_version=policy["version"], spec=spec)
                operation = reserved["operation"]
                request.state.usage_operation_id = str(operation["operation_id"])
                request.state.usage_operation_state = operation["state"]
                if not reserved["created"]:
                    return _replay(operation)
                executor = "sync:" + uuid4().hex
                dispatch = await _call(repository, "begin_dispatch", owner_user_id=owner,
                    operation_id=operation["operation_id"], generation=operation["generation"], executor_id=executor)
                operation = dispatch["operation"]
                request.state.usage_operation_state = operation["state"]
                if not dispatch["transitioned"]:
                    return _replay(operation)
                claim = await _call(repository, "claim_running", owner_user_id=owner,
                    operation_id=operation["operation_id"], generation=operation["generation"], executor_id=executor)
                operation = claim["operation"]
                request.state.usage_operation_state = operation["state"]
                if not claim["transitioned"]:
                    return _replay(operation)
                execution_started = True
                try:
                    result = await invoke(*args, **kwargs)
                except HTTPException as exc:
                    if module == "online_knowledge" and exc.status_code >= 500:
                        with anyio.CancelScope(shield=True):
                            marked = await _call(repository, "mark_unknown", owner_user_id=owner,
                                operation_id=operation["operation_id"], generation=operation["generation"],
                                reason="online_provider_or_result_unconfirmed")
                        request.state.usage_operation_state = marked["operation"]["state"]
                        exc.headers = {**(exc.headers or {}), **operation_header(marked["operation"])}
                        raise
                    # These local endpoints await their bounded execution before
                    # raising. Preserve the public failure for idempotent replay.
                    record = {"http_status": exc.status_code, "payload": {"detail": jsonable_encoder(exc.detail)}}
                    with anyio.CancelScope(shield=True):
                        await _call(repository, "settle", owner_user_id=owner,
                            operation_id=operation["operation_id"], generation=operation["generation"],
                            outcome="rejected" if 400 <= exc.status_code < 500 else "failed", qualified=False,
                            result_json=record)
                    request.state.usage_operation_state = "released"
                    exc.headers = {**(exc.headers or {}), **operation_header(operation)}
                    raise
                data = jsonable_encoder(result)
                frozen = await _call(repository, "get_policy", policy_version=operation["policy_version"])
                decision = qualify(frozen, module, operation["spec"], _facts(module, body, data, request), "completed")
                if not decision["terminal"]:
                    await _call(repository, "mark_unknown", owner_user_id=owner,
                        operation_id=operation["operation_id"], generation=operation["generation"],
                        reason="result_evidence_missing")
                    request.state.usage_operation_state = "unknown"
                    raise HTTPException(503, detail={"code": "usage_result_unconfirmed"},
                                        headers=operation_header(operation))
                record = {"http_status": 200, "payload": data}
                try:
                    size = len(json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",",":"), allow_nan=False).encode())
                    failure = "result_retention_limit" if size > frozen["result_max_bytes"] else None
                except (ValueError,TypeError):
                    failure = "invalid_result"
                if failure:
                    record = {"http_status":502, "payload":{"detail":{"code":failure,
                              "message":"The operation did not produce a retainable result."}}}
                    decision = {"outcome":failure,"qualified":False}
                with anyio.CancelScope(shield=True):
                    settled = await _call(repository, "settle", owner_user_id=owner,
                        operation_id=operation["operation_id"], generation=operation["generation"],
                        outcome=decision["outcome"], qualified=decision["qualified"], result_json=record)
                request.state.usage_operation_state = settled["operation"]["state"]
                return JSONResponse(record["payload"], status_code=record["http_status"],
                                    headers=operation_header(settled["operation"]))
            except core.EntitlementError as exc:
                if operation is not None:
                    with anyio.CancelScope(shield=True):
                        try:
                            current = await _call(repository, "get", owner_user_id=owner,
                                                  operation_id=operation["operation_id"])
                            if current and current["state"] not in core.TERMINAL_STATES:
                                current = (await _call(repository, "mark_unknown", owner_user_id=owner,
                                    operation_id=current["operation_id"], generation=current["generation"],
                                    reason="entitlement_ack_unconfirmed"))["operation"]
                            if current:
                                request.state.usage_operation_state = current["state"]
                        except Exception:
                            pass
                failure = public_entitlement_error(exc)
                if operation is not None:
                    failure.headers = {**(failure.headers or {}), **operation_header(operation)}
                raise failure from None
            except HTTPException:
                raise
            except BaseException:
                if operation is not None:
                    # A database ACK may be lost after commit. Never execute a
                    # second time or release uncertain in-flight work here.
                    with anyio.CancelScope(shield=True):
                        try:
                            current = await _call(repository, "get", owner_user_id=owner,
                                                  operation_id=operation["operation_id"])
                            if current and current["state"] not in core.TERMINAL_STATES:
                                if not execution_started and current["state"] == "reserved":
                                    await _call(repository, "release", owner_user_id=owner,
                                        operation_id=current["operation_id"], generation=current["generation"],
                                        reason="local_dispatch_not_started", proof={"kind": "not_dispatched"})
                                else:
                                    await _call(repository, "mark_unknown", owner_user_id=owner,
                                        operation_id=current["operation_id"], generation=current["generation"],
                                        reason="local_result_unconfirmed")
                        except Exception:
                            pass  # Last durable state keeps the reservation held.
                raise
            finally:
                upload = arguments.get("image")
                if upload is not None:
                    await upload.close()
        wrapped.__signature__ = signature
        return wrapped
    return decorate
