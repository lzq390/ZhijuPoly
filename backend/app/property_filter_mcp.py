"""Independent development MCP server. Run with ``python -m app.property_filter_mcp``."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from pydantic import TypeAdapter, ValidationError
from mcp import types
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.models import PropertyFilterOption, PropertyFilterSearchRequest
from app.property_filter_mcp_contract import (
    INPUT_MODELS, MAX_RESPONSE_BYTES, OUTPUT_MODELS, TOOL_NAMES,
    ErrorDetail, ErrorOutput, EvidenceOutput, HistogramInput, HistogramOutput,
    ListInput, ListOutput, MaterialOutput, MCPFailure, MeasurementOutput,
    SearchInput, SearchOutput, bind_condition, check_revision, dataset_revision,
    option_unit, public_option,
)
from app.property_filter_mcp_security import MCPConfig, CallLimiter, ClientCredential, ClientRegistry, RequestGuard
from app.services.property_filter import (
    PropertyFilterCatalogUnavailable, PropertyFilterOptionNotFound,
    get_property_filter_histogram_data, load_property_filter_context,
    resolve_property_filter_option, search_property_filter_data,
)

LOGGER = logging.getLogger("nexpoly.property_filter_mcp.audit")
ALLOWED_TABLES = frozenset({
    "core.polymer_property_filter_records", "governance.property_filter_options_snapshots",
    "governance.import_batches", "governance.source_files",
})
DESCRIPTIONS = {
    "list_filter_properties": "List numeric polymer-property options, exact units, ranges and dataset revision. Fetch this first; supply the same revision on later pages. No histograms are embedded.",
    "search_polymers": "Find polymer structures whose grouped measurements satisfy every bound. AND can match different samples or experiments, not simultaneous performance of one sample. Keywords are name/SMILES substrings; no unit conversion. Evidence may contain observed, median or imputed values. Use exact option_key and dataset_revision from the catalog.",
    "get_property_histogram": "Get the entire dataset distribution for one exact property option and unit. This is not a distribution of a multi-condition search result.",
}


class QueryControl:
    """Cancel timed-out work and prevent a late connection from starting a query."""
    def __init__(self, timeout_seconds: float = 30.0) -> None:
        self.deadline = time.monotonic() + timeout_seconds
        self.lock = threading.Lock()
        self.connection: Any = None
        self.cancelled = False

    def attach(self, connection: Any) -> None:
        with self.lock:
            if self.cancelled or time.monotonic() >= self.deadline:
                raise MCPFailure("QUERY_TIMEOUT", "Tool call timed out; narrow the query and retry.")
            self.connection = connection

    def detach(self) -> None:
        with self.lock:
            self.connection = None

    def checkpoint(self) -> float:
        with self.lock:
            remaining = self.deadline - time.monotonic()
            if self.cancelled or remaining <= 0:
                raise MCPFailure("QUERY_TIMEOUT", "Tool call timed out; narrow the query and retry.")
            return remaining

    def mark_cancelled(self) -> Any:
        # Synchronous: queued workers cannot attach while the I/O pool is busy.
        with self.lock:
            self.cancelled = True
            return self.connection

    @staticmethod
    def cancel_connection(connection: Any) -> None:
        if connection is not None:
            try:
                connection.cancel_safe(timeout=2)
            except psycopg.Error:
                pass

    def cancel(self) -> None:
        self.cancel_connection(self.mark_cancelled())


class ControlledConnection:
    """Check the whole-call deadline before every shared-service SQL statement."""
    def __init__(self, connection: Any, control: QueryControl, statement_timeout_ms: int):
        self.connection = connection
        self.control = control
        self.statement_timeout_ms = statement_timeout_ms

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        remaining_ms = max(1, int(self.control.checkpoint() * 1000))
        self.connection.execute("SELECT set_config('statement_timeout', %s, true)",
                                (str(min(self.statement_timeout_ms, remaining_ms)),))
        self.control.checkpoint()
        return self.connection.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)


class MCPRuntime:
    def __init__(self, config: MCPConfig):
        self.config = config
        self.limiter = CallLimiter(config)
        self.ready = False

    @contextmanager
    def transaction(self, control: QueryControl | None = None) -> Iterator[Any]:
        # No application settings/default DSN, pool or runtime model initialization.
        with psycopg.connect(self.config.dsn, row_factory=dict_row, connect_timeout=5,
                             application_name="nexpoly_property_filter_mcp", autocommit=True) as connection:
            if control is not None:
                control.attach(connection)
            try:
                with connection.transaction():
                    connection.execute("SET TRANSACTION READ ONLY")
                    connection.execute("SELECT set_config('statement_timeout', %s, true)", (str(self.config.statement_timeout_ms),))
                    connection.execute("SELECT set_config('lock_timeout', %s, true)", (str(self.config.lock_timeout_ms),))
                    # Import TRUNCATE/replacement cannot commit between revision and data reads.
                    connection.execute("LOCK TABLE core.polymer_property_filter_records IN ACCESS SHARE MODE")
                    yield ControlledConnection(connection, control, self.config.statement_timeout_ms) if control is not None else connection
            finally:
                if control is not None:
                    control.detach()

    def startup_check(self) -> None:
        ClientRegistry.read(self.config.clients_file)
        with self.transaction() as connection:
            role = connection.execute("""
                SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
                FROM pg_roles WHERE rolname = current_user
            """).fetchone()
            if role is None or any(role.values()):
                raise RuntimeError("MCP database role has administrative privileges")
            if connection.execute("SHOW default_transaction_read_only").fetchone()["default_transaction_read_only"] != "on":
                raise RuntimeError("MCP database role must default to read-only transactions")
            if connection.execute("SELECT has_database_privilege(current_user, current_database(), 'CREATE') AS allowed").fetchone()["allowed"]:
                raise RuntimeError("MCP database role must not create schemas")
            memberships = connection.execute("""
                SELECT 1 FROM pg_roles WHERE rolname <> current_user
                AND pg_has_role(current_user, oid, 'MEMBER') LIMIT 1
            """).fetchone()
            if memberships is not None:
                raise RuntimeError("MCP database role must not inherit other roles")
            privileges = connection.execute("""
                SELECT ns.nspname || '.' || cls.relname AS relation,
                       has_table_privilege(current_user, cls.oid, 'SELECT') AS can_read,
                       (has_table_privilege(current_user, cls.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                        OR has_any_column_privilege(current_user, cls.oid, 'INSERT,UPDATE,REFERENCES')) AS can_write
                FROM pg_class cls JOIN pg_namespace ns ON ns.oid = cls.relnamespace
                WHERE ns.nspname NOT LIKE 'pg_%' AND ns.nspname <> 'information_schema'
                  AND cls.relkind IN ('r', 'p', 'v', 'm', 'f')
            """).fetchall()
            readable = {row["relation"] for row in privileges if row["can_read"]}
            if readable != ALLOWED_TABLES or any(row["can_write"] for row in privileges):
                raise RuntimeError("MCP role must have SELECT only on the four allowed tables")
            if connection.execute("""
                SELECT 1 FROM pg_namespace WHERE nspname NOT LIKE 'pg_%'
                AND nspname <> 'information_schema' AND has_schema_privilege(current_user, oid, 'CREATE') LIMIT 1
            """).fetchone() is not None:
                raise RuntimeError("MCP role must not create schema objects")
            self._context(connection)
        self.ready = True

    @staticmethod
    def _context(connection: Any) -> Any:
        context = load_property_filter_context(connection, allow_live_fallback=False)
        if context.catalog is None or context.source_status not in {"ready", "empty"}:
            raise MCPFailure("CATALOG_UNAVAILABLE", "Current ready dataset catalog is unavailable; contact the data operator.")
        return context

    def execute(self, name: str, arguments: Any, control: QueryControl) -> Any:
        with self.transaction(control) as connection:
            context = self._context(connection)
            revision = dataset_revision(context.catalog)
            check_revision(arguments.dataset_revision, revision)
            if name == "list_filter_properties":
                assert isinstance(arguments, ListInput)
                options = [PropertyFilterOption.model_validate(item) for item in context.options]
                query = arguments.q.casefold().strip()
                matched = [option for option in options if not query or any(
                    query in str(value or "").casefold() for value in
                    (option.option_key, option.label, option.property_key, option.property_name, option_unit(option))
                )]
                start = (arguments.page - 1) * arguments.page_size
                return ListOutput(
                    dataset_revision=revision, source_status=context.source_status, total_measurement_count=context.total_records,
                    total_option_count=len(options), matched_option_count=len(matched),
                    page=arguments.page, page_size=arguments.page_size,
                    has_more=start + arguments.page_size < len(matched),
                    options=[public_option(option) for option in matched[start:start + arguments.page_size]],
                )
            if name == "get_property_histogram":
                assert isinstance(arguments, HistogramInput)
                option = resolve_property_filter_option(context, arguments.option_key)
                histogram, _ = get_property_filter_histogram_data(connection, context, arguments.option_key)
                return HistogramOutput(dataset_revision=revision, source_status=context.source_status, option_key=arguments.option_key,
                                       unit=option_unit(option), histogram=histogram)
            assert isinstance(arguments, SearchInput)
            options = [resolve_property_filter_option(context, item.option_key) for item in arguments.filters]
            request = PropertyFilterSearchRequest(
                filters=[bind_condition(option, item) for option, item in zip(options, arguments.filters)],
                q=arguments.q, page=arguments.page, page_size=arguments.page_size,
            )
            search = search_property_filter_data(connection, request,
                                                 measurement_limit_per_filter=arguments.measurement_limit_per_filter)
            results = []
            for material, group in zip(search.response.results, search.groups):
                evidence = []
                for index, option in enumerate(options):
                    measurements = [MeasurementOutput(**record.model_dump(), record_ref=f"{revision}:{record.filter_record_id}")
                                    for record in material.records if record.filter_index == index]
                    count = group.measurement_counts[index]
                    evidence.append(EvidenceOutput(filter_index=index, option_key=option.option_key,
                                                   unit=option_unit(option), matching_measurement_count=count,
                                                   truncated=count > len(measurements), measurements=measurements))
                group_hash = hashlib.sha256(group.group_key.encode()).hexdigest()
                results.append(MaterialOutput(
                    material_ref=f"{revision}:{group_hash}", smiles=material.smiles,
                    canonical_smiles=material.canonical_smiles, polymer_name=material.polymer_name,
                    matched_filters=material.matched_filters, matching_evidence_count=group.total_measurement_count,
                    evidence=evidence,
                ))
            return SearchOutput(
                dataset_revision=revision, source_status=context.source_status, total_measurement_count=search.response.total_records,
                matched_material_count=search.response.matched_records, page=arguments.page,
                page_size=arguments.page_size,
                has_more=arguments.page * arguments.page_size < search.response.matched_records,
                measurement_limit_per_filter=arguments.measurement_limit_per_filter, results=results,
            )

    async def call(self, context: Any, parameters: Any) -> types.CallToolResult:
        start = time.perf_counter()
        client: ClientCredential = context.request.state.mcp_client
        request_id = context.request.state.mcp_request_id
        name = parameters.name
        status = "ok"
        count = 0
        acquired = False
        deferred_release = False
        task: asyncio.Task[Any] | None = None
        control = QueryControl(self.config.tool_timeout_seconds)
        try:
            if name not in TOOL_NAMES:
                raise MCPFailure("UNKNOWN_TOOL", "Unknown tool; discover the available tools first.")
            if name not in client.tools:
                raise MCPFailure("PERMISSION_DENIED", "This client is not permitted to call the requested tool.")
            try:
                arguments = INPUT_MODELS[name].model_validate(parameters.arguments or {})
            except ValidationError:
                raise MCPFailure("INVALID_ARGUMENT", "Invalid tool arguments; use the published schema, finite ordered bounds and a revision for later pages.") from None
            self.limiter.acquire(client.client_id)
            acquired = True
            task = asyncio.create_task(asyncio.to_thread(self.execute, name, arguments, control))
            try:
                output = await asyncio.wait_for(asyncio.shield(task), timeout=self.config.tool_timeout_seconds)
            except (TimeoutError, asyncio.CancelledError):
                # Keep the slot until the worker actually exits; no orphan query can bypass concurrency.
                deferred_release = True
                def release_worker(finished: asyncio.Task[Any]) -> None:
                    self.limiter.release(client.client_id)
                    if not finished.cancelled():
                        finished.exception()
                task.add_done_callback(release_worker)
                connection_to_cancel = control.mark_cancelled()
                asyncio.create_task(asyncio.to_thread(control.cancel_connection, connection_to_cancel))
                raise MCPFailure("QUERY_TIMEOUT", "Tool call timed out; narrow the query and retry.")
            result = tool_result(output, request_id=context.request_id)
            payload = output.model_dump(mode="json")
            count = len(payload.get("results", payload.get("options", [])))
            return result
        except ValidationError:
            status = "DATA_UNAVAILABLE"
            return error_result(status, "Dataset contains invalid numeric or structured values; contact the data operator.")
        except PropertyFilterOptionNotFound:
            status = "UNKNOWN_OPTION"
            return error_result(status, "Unknown option_key; copy an exact key from the current property catalog.")
        except PropertyFilterCatalogUnavailable:
            status = "CATALOG_UNAVAILABLE"
            return error_result(status, "Current dataset catalog is missing, invalid or outdated; contact the data operator.")
        except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable):
            status = "QUERY_TIMEOUT"
            return error_result(status, "Query or dataset-lock wait timed out; narrow the query or retry later.")
        except MCPFailure as failure:
            status = failure.code
            return error_result(failure.code, failure.message)
        except psycopg.Error:
            status = "DATA_UNAVAILABLE"
            return error_result(status, "Dataset is temporarily unavailable; retry later.")
        except Exception:
            status = "INTERNAL_ERROR"
            return error_result(status, "Tool could not complete; contact the service operator with the request identifier.")
        finally:
            if acquired and not deferred_release:
                self.limiter.release(client.client_id)
            LOGGER.info(json.dumps({"client_id": client.client_id, "request_id": request_id,
                                    "tool": name if name in TOOL_NAMES else "unknown", "status": status,
                                    "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                                    "result_count": count}, separators=(",", ":")))


def tool_result(output: Any, *, request_id: Any = None) -> types.CallToolResult:
    payload = output.model_dump(mode="json")
    if isinstance(output, ListOutput):
        summary = f"Returned {len(output.options)} of {output.matched_option_count} property options. Use this dataset revision for subsequent calls."
    elif isinstance(output, SearchOutput):
        summary = f"Matched {output.matched_material_count} structures; returned {len(output.results)}. Conditions can match measurements from different samples or experiments; inspect evidence quality and truncation."
    else:
        summary = "Whole-dataset distribution for the selected exact property and unit."
    result = types.CallToolResult(content=[types.TextContent(type="text", text=summary)], structuredContent=payload)
    # Include protocol-envelope headroom; never silently shorten evidence or structure strings.
    envelope_size = len(json.dumps({"jsonrpc": "2.0", "id": request_id}, ensure_ascii=False).encode()) + 1024
    if len(result.model_dump_json(by_alias=True, exclude_none=True).encode()) + envelope_size > MAX_RESPONSE_BYTES:
        raise MCPFailure("RESPONSE_TOO_LARGE", "Response exceeds 256 KiB; reduce page_size or measurement_limit_per_filter, or narrow the query.")
    return result


def error_result(code: str, message: str) -> types.CallToolResult:
    payload = ErrorOutput(error=ErrorDetail(code=code, message=message)).model_dump(mode="json")
    return types.CallToolResult(content=[types.TextContent(type="text", text=f"{code}: {message}")],
                                structuredContent=payload, isError=True)


def create_app(config: MCPConfig | None = None) -> Any:
    config = config or MCPConfig.from_env()
    runtime = MCPRuntime(config)

    async def list_tools(context: Any, parameters: Any) -> types.ListToolsResult:
        client = context.request.state.mcp_client
        tools = []
        for name in ("list_filter_properties", "search_polymers", "get_property_histogram"):
            if name not in client.tools:
                continue
            output_schema = TypeAdapter(OUTPUT_MODELS[name] | ErrorOutput).json_schema()
            output_schema["type"] = "object"
            tools.append(types.Tool(name=name, description=DESCRIPTIONS[name],
                                    inputSchema=INPUT_MODELS[name].model_json_schema(), outputSchema=output_schema,
                                    annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                                                      idempotentHint=True, openWorldHint=False)))
        return types.ListToolsResult(tools=tools)

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok" if runtime.ready else "unavailable"}, status_code=200 if runtime.ready else 503)

    server = Server("nexpoly-property-filter", version="1.0.0", on_list_tools=list_tools, on_call_tool=runtime.call,
                    instructions="Read-only development polymer-property filtering. Fetch catalog keys and revision first. Cross-property AND is by structure, not by experimental sample.")
    # Do not emit tool parameters through tracing exporters installed elsewhere.
    server.middleware.clear()
    app = server.streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True,
        max_request_body_size=32 * 1024,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=list(config.allowed_hosts),
                                                     allowed_origins=list(config.allowed_origins)),
        custom_starlette_routes=[Route("/healthz", health, methods=["GET"])],
    )
    sdk_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Any) -> Any:
        await asyncio.to_thread(runtime.startup_check)
        async with sdk_lifespan(application):
            yield
        runtime.ready = False

    app.router.lifespan_context = lifespan
    app.add_middleware(RequestGuard, config=config)
    app.state.mcp_runtime = runtime
    return app


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    LOGGER.setLevel(logging.INFO)
    try:
        config = MCPConfig.from_env()
    except (OSError, ValueError):
        raise SystemExit("Invalid MCP configuration; verify explicit private DSN and client files.") from None
    uvicorn.run(create_app(config), host=config.host, port=config.port, access_log=False,
                log_level="warning", proxy_headers=False, timeout_keep_alive=5)


if __name__ == "__main__":
    main()
