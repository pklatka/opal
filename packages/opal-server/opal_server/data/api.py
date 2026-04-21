from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import RedirectResponse
from opal_common.authentication.authz import (
    require_peer_type,
    restrict_optional_topics_to_publish,
)
from opal_common.authentication.deps import JWTAuthenticator, get_token_from_header
from opal_common.authentication.types import JWTClaims
from opal_common.authentication.verifier import Unauthorized
from opal_common.logger import logger
from opal_common.schemas.data import (
    DataSourceConfig,
    DataSourceEntry,
    DataUpdate,
    DataUpdateReport,
    ServerDataSourceConfig,
)
from pydantic import Field as PydanticField


class DataUpdateWithExtension(DataUpdate):
    """DataUpdate extended with Symphony extension fields.

    All extension fields are optional with safe defaults so existing
    callers that send a plain DataUpdate body continue to work unchanged.
    """

    extension_level: str = PydanticField(
        default="L0",
        description="Symphony extension level (L0-L3)",
    )
    extension_code: Optional[str] = PydanticField(
        default=None,
        description="Python extension code for L1/L2",
    )
    task_description: Optional[str] = PydanticField(
        default=None,
        description="Natural-language task description for L2/L3",
    )
    execution_mode: str = PydanticField(
        default="direct",
        description="Execution mode: direct or goex",
    )
    reversal_code: Optional[str] = PydanticField(
        default=None,
        description="Undo code for GoEx mode",
    )
from opal_common.schemas.security import PeerType
from opal_common.urls import set_url_query_param
from opal_server.config import opal_server_config
from opal_server.data.data_update_publisher import DataUpdatePublisher
from symphony import tool, handle_extension

from opal_server.symphony_ext import (
    post_data_update,
    _data_update_capabilities,
    goex_registry,
)


def _default_publish_data_update(update: DataUpdate) -> list[dict]:
    """Default L0 data update logic — serialize entries to dicts."""
    return [
        e.dict() if hasattr(e, "dict") else e.model_dump()
        for e in update.entries
    ]


def _normalize_extension_entry_aliases(item: dict) -> dict:
    """Coerce common extension output aliases back into DataSourceEntry shape."""
    normalized = dict(item)
    if "topics" not in normalized and isinstance(normalized.get("topic"), str):
        normalized["topics"] = [normalized["topic"]]
    if "dst_path" not in normalized and isinstance(normalized.get("path"), str):
        normalized["dst_path"] = normalized["path"]
    if "url" not in normalized and isinstance(normalized.get("source"), str):
        normalized["url"] = normalized.pop("source")
    data_source = normalized.pop("data_source", None)
    if isinstance(data_source, dict):
        if "url" not in normalized and isinstance(data_source.get("url"), str):
            normalized["url"] = data_source["url"]
        if "save_method" not in normalized and isinstance(data_source.get("save_method"), str):
            normalized["save_method"] = data_source["save_method"]
    return normalized


def _callback_urls(update: DataUpdate) -> list[str]:
    urls: list[str] = []
    for callback in update.callback.callbacks:
        if isinstance(callback, (list, tuple)):
            urls.append(str(callback[0]))
        else:
            urls.append(str(callback))
    return urls


def init_data_updates_router(
    data_update_publisher: DataUpdatePublisher,
    data_sources_config: ServerDataSourceConfig,
    authenticator: JWTAuthenticator,
):
    router = APIRouter()

    @router.get(opal_server_config.ALL_DATA_ROUTE)
    async def default_all_data():
        """A fake data source configured to be fetched by the default data
        source config.

        If the user deploying OPAL did not set DATA_CONFIG_SOURCES
        properly, OPAL clients will be hitting this route, which will
        return an empty dataset (empty dict).
        """
        logger.warning(
            "Serving default all-data route, meaning DATA_CONFIG_SOURCES was not configured!"
        )
        return {}

    @router.post(
        opal_server_config.DATA_CALLBACK_DEFAULT_ROUTE,
        dependencies=[Depends(authenticator)],
    )
    async def log_client_update_report(report: DataUpdateReport):
        """A data update callback to be called by the OPAL client after
        completing an update.

        If the user deploying OPAL-client did not set
        OPAL_DEFAULT_UPDATE_CALLBACKS properly, this method will be
        called as the default callback (will simply log the report).
        """
        logger.info(
            "Received update report: {report}",
            report=report.dict(
                exclude={"reports": {"__all__": {"entry": {"config", "data"}}}}
            ),
        )
        return {}  # simply returns 200

    @tool(
        name="get_data_sources_config",
        method="GET",
        path=opal_server_config.DATA_CONFIG_ROUTE,
    )
    @router.get(
        opal_server_config.DATA_CONFIG_ROUTE,
        response_model=DataSourceConfig,
        responses={
            307: {
                "description": "The data source configuration is available at another location (redirect)"
            },
        },
        dependencies=[Depends(authenticator)],
    )
    async def get_data_sources_config(authorization: Optional[str] = Header(None)):
        """Provides OPAL clients with their base data config, meaning from
        where they should fetch a *complete* picture of the policy data they
        need.

        Clients will use this config to pull all data when they
        initially load and when they are reconnected to server after a
        period of disconnection (in which they cannot receive
        incremental updates).
        """
        token = get_token_from_header(authorization)
        if data_sources_config.config is not None:
            logger.info("Serving source configuration")
            return data_sources_config.config
        elif data_sources_config.external_source_url is not None:
            url = str(data_sources_config.external_source_url)
            short_token = token[:5] + "..." + token[-5:]
            logger.info(
                "Source configuration is available at '{url}', redirecting with token={token} (abbrv.)",
                url=url,
                token=short_token,
            )
            redirect_url = set_url_query_param(url, "token", token)
            return RedirectResponse(url=redirect_url)
        else:
            logger.error("pydantic model invalid", model=data_sources_config)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Did not find a data source configuration!",
            )

    @tool(
        name="publish_data_update",
        method="POST",
        path=opal_server_config.DATA_CONFIG_ROUTE,
        levels=["L0", "L1", "L2", "L3"],
        level_params={
            "L0": ["entries", "reason", "id", "callback"],
            "L1": ["entries", "reason", "id", "callback", "extension_level", "extension_code", "execution_mode", "reversal_code"],
            "L2": ["entries", "reason", "id", "callback", "extension_level", "extension_code", "task_description", "execution_mode", "reversal_code"],
            "L3": ["entries", "reason", "id", "callback", "extension_level", "task_description", "execution_mode", "reversal_code"],
        },
        level_overrides={
            "L0": {
                "description": (
                    "Publish a data update to OPAL clients. Inputs: entries, reason, id, callback. "
                    "Each entry carries topics, url, dst_path/path, and save_method; response includes status and callback_urls."
                ),
            },
            "L1": {
                "description": (
                    "Same as L0 plus extension_code and reversal_code. Extension code receives candidate entries "
                    "before publish and should validate, filter, deduplicate, or transform them."
                ),
            },
            "L2": {
                "description": (
                    "Same as L1 plus task_description for server-side codegen before publish. "
                    "Use for production-safe candidate filtering while preserving callback and save_method requirements."
                ),
            },
            "L3": {
                "description": (
                    "Same as L2, but source-aware: task_description drives codegen using endpoint source and entry context. "
                    "Return only entries that should be published."
                ),
            },
        },
    )
    @router.post(opal_server_config.DATA_CONFIG_ROUTE)
    async def publish_data_update_event(
        update: DataUpdateWithExtension,
        claims: JWTClaims = Depends(authenticator),
    ):
        """Publish incremental policy data updates to OPAL clients.

        Extension levels:
        - **L0**: Publish entries as-is to subscribed clients
        - **L1**: Post-processing via extension_code (validate, filter, deduplicate)
        - **L2**: Auto-generated extension code for advanced entry processing
        - **L3**: Source-aware — LLM reads endpoint code and generates extensions
        - **L4**: Freeform extension on the /data/config route using the live update context

        Extension fields (extension_level, extension_code, task_description,
        execution_mode, reversal_code) are part of the JSON request body to
        avoid URL length limits on large extension_code payloads.
        """
        extension_level = update.extension_level
        extension_code = update.extension_code
        task_description = update.task_description
        execution_mode = update.execution_mode
        reversal_code = update.reversal_code

        try:
            require_peer_type(
                authenticator, claims, PeerType.datasource
            )  # may throw Unauthorized
            restrict_optional_topics_to_publish(
                authenticator, claims, update
            )  # may throw Unauthorized
        except Unauthorized as e:
            logger.error(f"Unauthorized to publish update: {repr(e)}")
            raise

        entries_data = _default_publish_data_update(update)
        context = {
            "entries": entries_data,
            "reason": update.reason,
            "entry_count": len(entries_data),
        }

        outcome = await handle_extension(
            level=extension_level,
            extension_code=extension_code,
            task_description=task_description,
            execution_mode=execution_mode,
            reversal_code=reversal_code,
            extension_point=post_data_update,
            default_fn=lambda: entries_data,
            context=context,
            all_capabilities=_data_update_capabilities,
            goex_registry=goex_registry,
            original_call='result = context["entries"]',
            default_source=_default_publish_data_update,
            endpoint_path="/data/config",
            trigger_condition=lambda res: bool(extension_code) or bool(task_description),
        )

        ext = outcome.ext_result

        # If extension triggered, rebuild update with filtered/modified entries
        if ext.triggered and isinstance(outcome.results, list):
            try:
                new_entries = [
                    DataSourceEntry(
                        **{
                            k: v
                            for k, v in _normalize_extension_entry_aliases(e).items()
                            if v is not None
                        }
                    )
                    if isinstance(e, dict) else e
                    for e in outcome.results
                ]
                update = DataUpdate(
                    id=update.id,
                    entries=new_entries,
                    reason=update.reason,
                    callback=update.callback,
                )
            except Exception as e:
                logger.warning(f"Extension returned invalid entries: {e}")

        if data_update_publisher is not None:
            await data_update_publisher.publish_data_updates(update)
        else:
            logger.warning("Data update publisher not configured; update not broadcast")

        response: dict = {"status": "ok"}
        response["callback_urls"] = _callback_urls(update)
        if outcome.needs_extension:
            response["needs_extension"] = True
            if outcome.extension_context:
                response["extension_context"] = outcome.extension_context
        if ext.triggered:
            response["extension_triggered"] = True
            response["generated_code"] = ext.generated_code or extension_code
            response["endpoint_source"] = ext.endpoint_source
            response["entries_published"] = len(update.entries)
        if ext.goex_record_id:
            response["goex_record_id"] = ext.goex_record_id
            response["goex_mode"] = execution_mode == "goex"
            response["goex_reversal_code"] = ext.goex_reversal_code
        return response

    return router
