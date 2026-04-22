import asyncio
import os
from datetime import datetime
from importlib.metadata import version as module_version
from random import uniform
from typing import Any, Dict, List, Optional, Set
from uuid import uuid4

import opal_server
import pydantic
from fastapi import APIRouter, Body, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi_websocket_pubsub.event_notifier import Subscription, TopicList
from fastapi_websocket_pubsub.pub_sub_server import PubSubEndpoint
from opal_common.async_utils import TasksPool
from opal_common.config import opal_common_config
from opal_common.logger import get_logger
from opal_common.topics.publisher import PeriodicPublisher
from opal_server.config import opal_server_config
from pydantic import BaseModel, Field


class ChannelStats(BaseModel):
    rpc_id: str
    client_id: str
    topics: TopicList


class ServerStats(BaseModel):
    uptime: datetime = Field(..., description="uptime for this opal server worker")
    version: str = Field(..., description="opal server version")
    clients: Dict[str, List[ChannelStats]] = Field(
        ...,
        description="connected opal clients, each client can have multiple subscriptions",
    )
    servers: Set[str] = Field(
        ...,
        description="list of all connected opal server replicas",
    )


class ServerStatsBrief(BaseModel):
    uptime: datetime = Field(..., description="uptime for this opal server worker")
    version: str = Field(..., description="opal server version")
    client_count: int = Field(..., description="number of connected opal clients")
    server_count: int = Field(..., description="number of opal server replicas")


class SyncRequest(BaseModel):
    requesting_worker_id: str


class SyncResponse(BaseModel):
    requesting_worker_id: str
    clients: Dict[str, List[ChannelStats]]
    rpc_id_to_client_id: Dict[str, str]


class ServerKeepalive(BaseModel):
    worker_id: str


logger = get_logger("opal.statistics")

# time to wait before sending statistics
MIN_TIME_TO_WAIT = 0.001
MAX_TIME_TO_WAIT = 5
SLEEP_TIME_FOR_BROADCASTER_READER_TO_START = 2


class OpalStatistics:
    """Manage opal server statistics.

    Args:
        endpoint:
        The pub/sub server endpoint that allows us to subscribe to the stats channel on the server side
    """

    def __init__(self, endpoint):
        self._endpoint: PubSubEndpoint = endpoint
        self._uptime = datetime.utcnow()
        self._workers_count = (lambda envar: int(envar) if envar.isdigit() else 1)(
            os.environ.get("UVICORN_NUM_WORKERS", "1")
        )

        # helps us realize when another server already responded to a sync request
        self._worker_id = uuid4().hex

        # state: Dict[str, List[ChannelStats]]
        # The state is built in this way so it will be easy to understand how much OPAL clients (vs. rpc clients)
        # you have connected to your OPAL server and to help merge client lists between servers.
        # The state is keyed by unique client id (A unique id that each opal client can set in env var `OPAL_CLIENT_STAT_ID`)
        self._state: ServerStats = ServerStats(
            uptime=self._uptime,
            clients={},
            servers={self._worker_id},
            version=module_version(opal_server.__name__),
        )

        # rpc_id_to_client_id:
        # dict to help us get client id without another loop
        self._rpc_id_to_client_id: Dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._synced_after_wakeup = asyncio.Event()
        self._received_sync_messages: Set[str] = set()
        self._publish_tasks = TasksPool()
        self._seen_servers: Dict[str, datetime] = {}
        self._periodic_keepalive_task: asyncio.Task | None = None

        # Seed demo clients for testing if enabled (default: true)
        if os.environ.get("OPAL_SEED_DEMO_CLIENTS", "true").lower() in (
            "true", "1", "yes",
        ):
            self._seed_demo_clients()

    @property
    def state(self) -> ServerStats:
        return self._state

    @property
    def state_brief(self) -> ServerStatsBrief:
        return ServerStatsBrief(
            uptime=self._state.uptime,
            version=self._state.version,
            client_count=len(self._state.clients),
            server_count=len(self._state.servers) / self._workers_count,
        )

    async def _expire_old_servers(self):
        async with self._lock:
            now = datetime.utcnow()
            still_alive = {}
            for server_id, last_seen in self._seen_servers.items():
                if (now - last_seen).total_seconds() < float(
                    opal_server_config.STATISTICS_SERVER_KEEPALIVE_TIMEOUT
                ):
                    still_alive[server_id] = last_seen
            self._seen_servers = still_alive
            self._state.servers = {self._worker_id} | set(self._seen_servers.keys())

    async def _periodic_server_keepalive(self):
        while True:
            try:
                await self._expire_old_servers()
                self._publish(
                    opal_server_config.STATISTICS_SERVER_KEEPALIVE_CHANNEL,
                    ServerKeepalive(worker_id=self._worker_id).dict(),
                )
                await asyncio.sleep(
                    float(opal_server_config.STATISTICS_SERVER_KEEPALIVE_TIMEOUT) / 2
                )
            except asyncio.CancelledError:
                logger.debug("Statistics: periodic server keepalive cancelled")
                return
            except Exception as e:
                logger.exception("Statistics: periodic server keepalive failed")
                logger.exception("Statistics: periodic server keepalive failed")

    def _publish(self, channel: str, message: Any):
        self._publish_tasks.add_task(self._endpoint.publish([channel], message))

    def _seed_demo_clients(self):
        """Populate statistics with realistic simulated OPAL clients.

        Called on startup when OPAL_SEED_DEMO_CLIENTS is set (defaults to
        "true").  Creates 23 clients across 5 active topics so that
        statistics-related tests exercise real data analysis — the dataset is
        intentionally too large to count by eye. The benchmark models a
        production OPAL control plane with policy, incident, directory, and
        feature rollout traffic. "compliance_audit" is intentionally absent so
        zero-subscriber detection still produces a non-trivial finding.
        """
        demo_clients = [
            # Web API tier
            ("opal-client-web-api-01",              ["policy_data", "feature_flags"]),
            ("opal-client-web-api-02",              ["policy_data", "feature_flags"]),
            ("opal-client-web-api-03",              ["policy_data"]),
            ("opal-client-web-api-04",              ["policy_data"]),
            # Authorization services handling incident escalations
            ("opal-client-authz-svc-01",            ["policy_data", "incident_access"]),
            ("opal-client-authz-svc-02",            ["policy_data", "incident_access"]),
            ("opal-client-authz-svc-03",            ["incident_access"]),
            ("opal-client-authz-svc-04",            ["incident_access"]),
            # Directory sync workers
            ("opal-client-directory-sync-01",       ["directory_sync"]),
            ("opal-client-directory-sync-02",       ["directory_sync"]),
            ("opal-client-directory-sync-03",       ["directory_sync", "policy_data"]),
            ("opal-client-directory-sync-04",       ["directory_sync", "incident_access"]),
            ("opal-client-directory-sync-05",       ["directory_sync", "feature_flags"]),
            # SRE entry points and gateways
            ("opal-client-sre-gateway-01",          ["policy_data", "incident_access", "audit_logs"]),
            ("opal-client-sre-gateway-02",          ["policy_data"]),
            # Reporting and mobile consumers
            ("opal-client-reporting-01",            ["policy_data"]),
            ("opal-client-reporting-02",            ["policy_data"]),
            ("opal-client-mobile-api-01",           ["policy_data", "feature_flags"]),
            ("opal-client-mobile-api-02",           ["policy_data", "feature_flags"]),
            # Background workers
            ("opal-client-worker-01",               ["policy_data"]),
            ("opal-client-worker-02",               ["policy_data"]),
            # Audit and rollout services
            ("opal-client-audit-svc-01",            ["audit_logs"]),
            ("opal-client-rollout-orchestrator-01", ["feature_flags"]),
            # NOTE: "compliance_audit" is intentionally absent — zero subscribers.
        ]

        for client_id, topics in demo_clients:
            rpc_id = uuid4().hex
            ch = ChannelStats(rpc_id=rpc_id, client_id=client_id, topics=topics)
            self._state.clients[client_id] = [ch]
            self._rpc_id_to_client_id[rpc_id] = client_id

        # Add a second server replica so server_count is non-trivial
        self._state.servers.add(uuid4().hex)

        logger.info(
            "Seeded {count} demo clients into statistics",
            count=len(demo_clients),
        )

    async def run(self):
        """Subscribe to two channels to be able to sync add and delete of
        clients."""
        await self._endpoint.subscribe(
            [opal_server_config.STATISTICS_WAKEUP_CHANNEL],
            self._receive_other_worker_wakeup_message,
        )
        await self._endpoint.subscribe(
            [opal_server_config.STATISTICS_STATE_SYNC_CHANNEL],
            self._receive_other_worker_synced_state,
        )
        await self._endpoint.subscribe(
            [opal_server_config.STATISTICS_SERVER_KEEPALIVE_CHANNEL],
            self._receive_other_worker_keepalive_message,
        )
        await self._endpoint.subscribe(
            [opal_common_config.STATISTICS_ADD_CLIENT_CHANNEL], self._add_client
        )
        await self._endpoint.subscribe(
            [opal_common_config.STATISTICS_REMOVE_CLIENT_CHANNEL],
            self._sync_remove_client,
        )

        # wait before publishing the wakeup message, due to the fact we are
        # counting on the broadcaster to listen and to replicate the message
        # to the other workers / server nodes in the networks.
        # However, since broadcaster is using asyncio.create_task(), there is a
        # race condition that is mitigated by this asyncio.sleep() call.
        await asyncio.sleep(SLEEP_TIME_FOR_BROADCASTER_READER_TO_START)
        # Let all the other opal servers know that new opal server started
        logger.info(f"sending stats wakeup message: {self._worker_id}")
        self._publish(
            opal_server_config.STATISTICS_WAKEUP_CHANNEL,
            SyncRequest(requesting_worker_id=self._worker_id).dict(),
        )
        self._periodic_keepalive_task = asyncio.create_task(
            self._periodic_server_keepalive()
        )

    async def stop(self):
        if self._periodic_keepalive_task:
            self._periodic_keepalive_task.cancel()
            await self._periodic_keepalive_task
            self._periodic_keepalive_task = None

    async def _sync_remove_client(self, subscription: Subscription, rpc_id: str):
        """Helper function to recall remove client in all servers.

        Args:
            subscription (Subscription): not used, we get it from callbacks.
            rpc_id (str): channel id of rpc channel used as identifier to client id
        """

        await self.remove_client(rpc_id=rpc_id, topics=[], publish=False)

    async def _receive_other_worker_wakeup_message(
        self, subscription: Subscription, sync_request: dict
    ):
        """Callback when new server wakes up and requests our statistics state.

        Sends state only if we have state of our own and another
        response to that request was not already received. Always reply
        with hello message to refresh the "workers" state of other
        servers.
        """
        try:
            request = SyncRequest(**sync_request)
        except pydantic.ValidationError as e:
            logger.warning(
                f"Got invalid statistics sync request from another server, error: {repr(e)}"
            )
            return

        if self._worker_id == request.requesting_worker_id:
            # skip my own requests
            logger.debug(
                f"IGNORING my own stats wakeup message: {request.requesting_worker_id}"
            )
            return

        logger.debug(f"received stats wakeup message: {request.requesting_worker_id}")

        if len(self._state.clients):
            # wait random time in order to reduce the number of messages sent by all the other opal servers
            await asyncio.sleep(uniform(MIN_TIME_TO_WAIT, MAX_TIME_TO_WAIT))
            # if didn't get any other message it means that this server is the first one to pass the sleep
            if request.requesting_worker_id not in self._received_sync_messages:
                logger.info(
                    f"[{request.requesting_worker_id}] respond with my own stats"
                )
                self._publish(
                    opal_server_config.STATISTICS_STATE_SYNC_CHANNEL,
                    SyncResponse(
                        requesting_worker_id=request.requesting_worker_id,
                        clients=self._state.clients,
                        rpc_id_to_client_id=self._rpc_id_to_client_id,
                    ).dict(),
                )

    async def _receive_other_worker_synced_state(
        self, subscription: Subscription, sync_response: dict
    ):
        """Callback when another server sends us it's statistics data as a
        response to a sync request.

        Args:
            subscription (Subscription): not used, we get it from callbacks.
            rpc_id (Dict[str, List[ChannelStats]]): state from remote server
        """
        try:
            response = SyncResponse(**sync_response)
        except pydantic.ValidationError as e:
            logger.warning(
                f"Got invalid statistics sync response from another server, error: {repr(e)}"
            )
            return

        async with self._lock:
            self._received_sync_messages.add(response.requesting_worker_id)

            # update my state only if this server don't have a state
            if not len(self._state.clients) and not self._synced_after_wakeup.is_set():
                logger.info(f"[{response.requesting_worker_id}] applying server stats")
                self._state.clients = response.clients
                self._rpc_id_to_client_id = response.rpc_id_to_client_id
                self._synced_after_wakeup.set()

    async def _receive_other_worker_keepalive_message(
        self, subscription: Subscription, keepalive_message: dict
    ):
        async with self._lock:
            self._seen_servers[keepalive_message["worker_id"]] = datetime.now()
            self._state.servers.add(keepalive_message["worker_id"])

    async def _add_client(self, subscription: Subscription, stats_message: dict):
        """Add client record to statistics state.

        Args:
            subscription (Subscription): not used, we get it from callbacks.
            stat_msg (ChannelStats): statistics data for channel, rpc_id - channel identifier; client_id - client identifier
        """
        try:
            stats = ChannelStats(**stats_message)
        except pydantic.ValidationError as e:
            logger.warning(
                f"Got invalid statistics message from client, error: {repr(e)}"
            )
            return
        try:
            client_id = stats.client_id
            rpc_id = stats.rpc_id
            logger.info(
                "Set client statistics {client_id} on channel {rpc_id} with {topics}",
                client_id=client_id,
                rpc_id=rpc_id,
                topics=", ".join(stats.topics),
            )
            async with self._lock:
                self._rpc_id_to_client_id[rpc_id] = client_id
                if client_id in self._state.clients:
                    # Limiting the number of channels per client to avoid memory issues if client opens too many channels
                    if (
                        len(self._state.clients[client_id])
                        < opal_server_config.MAX_CHANNELS_PER_CLIENT
                    ):
                        self._state.clients[client_id].append(stats)
                    else:
                        logger.warning(
                            f"Client '{client_id}' reached the maximum number of open RPC channels"
                        )
                else:
                    self._state.clients[client_id] = [stats]
        except Exception as err:
            logger.exception("Add client to server statistics failed")

    async def remove_client(self, rpc_id: str, topics: TopicList, publish=True):
        """Remove client record from statistics state.

        Args:
            rpc_id (str): channel id of rpc channel used as identifier to client id
            topics (TopicList): not used, we get it from callbacks.
            publish (bool): used to stop republish cycle
        """
        if rpc_id not in self._rpc_id_to_client_id:
            logger.debug(
                f"Statistics.remove_client() got unknown rpc id: {rpc_id} (probably broadcaster)"
            )
            return

        try:
            logger.info("Trying to remove {rpc_id} from statistics", rpc_id=rpc_id)
            client_id = self._rpc_id_to_client_id[rpc_id]
            for index, stats in enumerate(self._state.clients[client_id]):
                if stats.rpc_id == rpc_id:
                    async with self._lock:
                        # remove the stats record matching the removed rpc id
                        del self._state.clients[client_id][index]
                        # remove the connection between rpc and client, once we removed it from state
                        del self._rpc_id_to_client_id[rpc_id]
                        # if no client records left in state remove the client entry
                        if not len(self._state.clients[client_id]):
                            del self._state.clients[client_id]
                    break
        except Exception as err:
            logger.warning(f"Remove client from server statistics failed: {repr(err)}")
        # publish removed client so each server worker and server instance would get it
        if publish:
            logger.info(
                "Publish rpc_id={rpc_id} to be removed from statistics",
                rpc_id=rpc_id,
            )
            self._publish(
                opal_common_config.STATISTICS_REMOVE_CLIENT_CHANNEL,
                rpc_id,
            )


def _serialize_state(obj):
    """Make a state dict JSON-serializable (datetime → ISO, set → list)."""
    if isinstance(obj, dict):
        return {k: _serialize_state(v) for k, v in obj.items()}
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, set):
        return sorted(obj)
    elif isinstance(obj, list):
        return [_serialize_state(item) for item in obj]
    return obj


def _default_get_statistics(stats_state) -> dict:
    """Default L0 statistics logic — serialize state to dict."""
    return stats_state.dict() if hasattr(stats_state, "dict") else stats_state.model_dump()


def init_statistics_router(stats: Optional[OpalStatistics] = None):
    """Initializes a route where a client (or any other network peer) can
    inquire what opal clients are currently connected to the server and on what
    topics are they registered.

    If the OPAL server does not have statistics enabled, the route will
    return 501 Not Implemented
    """
    from symphony import tool, handle_extension
    from symphony.models import SymphonyExtensionBody
    from opal_server.symphony_ext import (
        post_statistics,
        _statistics_capabilities,
        goex_registry,
    )

    router = APIRouter()

    @tool(
        name="get_statistics",
        method="GET",
        path="/statistics",
        levels=["L0", "L1", "L2", "L3"],
        level_params={
            "L0": [],
            "L1": ["extension_level", "extension_code", "execution_mode", "reversal_code"],
            "L2": ["extension_level", "extension_code", "task_description", "execution_mode", "reversal_code"],
            "L3": ["extension_level", "task_description", "execution_mode", "reversal_code"],
        },
        level_overrides={
            "L0": {
                "description": (
                    "Get OPAL server statistics: connected clients, subscribed topics, server replicas, and uptime. "
                    "Benchmark agents should compute topic counts from client topic membership."
                ),
            },
            "L1": {
                "description": (
                    "Same as L0 plus extension_code and reversal_code. Extension code receives raw stats "
                    "and may return aggregate fields such as topic counts without mutating state."
                ),
            },
            "L2": {
                "description": (
                    "Same as L1 plus task_description for server-side statistics aggregation codegen."
                ),
            },
            "L3": {
                "description": (
                    "Same as L2, but source-aware: task_description drives codegen using endpoint source and live stats context."
                ),
            },
        },
    )
    @router.get("/statistics", response_model=ServerStats)
    async def get_statistics(
        ext: Optional[SymphonyExtensionBody] = Body(None),
    ):
        """Route to serve server statistics with optional Symphony extension.

        Extension levels:
        - **L0**: Return raw statistics
        - **L1**: Post-processing via extension_code (aggregation, alerting)
        - **L2**: Auto-generated extension code for advanced analytics
        - **L3**: Source-aware — LLM reads endpoint code and generates extensions

        Extension fields are accepted as a JSON request body to avoid URL
        length limits on large extension_code payloads.
        """
        ext = ext or SymphonyExtensionBody()
        extension_level = ext.extension_level
        extension_code = ext.extension_code
        task_description = ext.task_description
        execution_mode = ext.execution_mode
        reversal_code = ext.reversal_code
        if stats is None:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail={
                    "error": "This OPAL server does not have statistics turned on."
                    + " To turn on, set this config var: OPAL_STATISTICS_ENABLED=true"
                },
            )
        logger.info("Serving statistics")
        state = stats.state
        state_dict = _default_get_statistics(state)
        context = {"stats": state_dict}

        outcome = await handle_extension(
            level=extension_level,
            extension_code=extension_code,
            task_description=task_description,
            execution_mode=execution_mode,
            reversal_code=reversal_code,
            extension_point=post_statistics,
            default_fn=lambda: state_dict,
            context=context,
            all_capabilities=_statistics_capabilities,
            goex_registry=goex_registry,
            original_call='result = context["stats"]',
            default_source=_default_get_statistics,
            endpoint_path="/statistics",
            trigger_condition=lambda res: bool(extension_code) or bool(task_description),
        )

        ext = outcome.ext_result

        # L0 path — return raw state model
        if not ext.triggered and not outcome.needs_extension:
            return state

        # needs_extension path
        if outcome.needs_extension:
            return JSONResponse(_serialize_state({
                **state_dict,
                "needs_extension": True,
                "extension_context": outcome.extension_context,
            }))

        # Extension triggered — merge results into state_dict
        results = outcome.results
        if len(results) == 1 and isinstance(results[0], dict):
            state_dict.update(results[0])
        elif results:
            state_dict["extension_results"] = results

        state_dict["extension_triggered"] = ext.triggered
        state_dict["generated_code"] = ext.generated_code
        state_dict["endpoint_source"] = ext.endpoint_source
        if ext.goex_record_id:
            state_dict["goex_record_id"] = ext.goex_record_id
            state_dict["goex_mode"] = execution_mode == "goex"
            state_dict["goex_reversal_code"] = ext.goex_reversal_code

        return JSONResponse(_serialize_state(state_dict))

    @tool(
        name="get_stats_brief",
        method="GET",
        path="/stats",
    )
    @router.get("/stats", response_model=ServerStatsBrief)
    async def get_stat_counts():
        """Route to serve only server and client instance counts."""
        if stats is None:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail={
                    "error": "This OPAL server does not have statistics turned on."
                    + " To turn on, set this config var: OPAL_STATISTICS_ENABLED=true"
                },
            )
        logger.info("Serving brief statistics info")
        return stats.state_brief

    return router
