# Copyright 2015, 2016 OpenMarket Ltd
# Copyright 2022 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import urllib.parse
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from prometheus_client import Counter
from typing_extensions import TypeGuard

from synapse.api.constants import EventTypes, Membership, ThirdPartyEntityKind
from synapse.api.errors import CodeMessageException
from synapse.appservice import (
    ApplicationService,
    TransactionOneTimeKeysCount,
    TransactionUnusedFallbackKeys,
)
from synapse.events import EventBase
from synapse.events.utils import SerializeEventConfig, serialize_event
from synapse.http.client import SimpleHttpClient
from synapse.types import DeviceListUpdates, JsonDict, ThirdPartyInstanceID
from synapse.util.caches.response_cache import ResponseCache

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

sent_transactions_counter = Counter(
    "synapse_appservice_api_sent_transactions",
    "Number of /transactions/ requests sent",
    ["service"],
)

failed_transactions_counter = Counter(
    "synapse_appservice_api_failed_transactions",
    "Number of /transactions/ requests that failed to send",
    ["service"],
)

sent_events_counter = Counter(
    "synapse_appservice_api_sent_events", "Number of events sent to the AS", ["service"]
)

sent_ephemeral_counter = Counter(
    "synapse_appservice_api_sent_ephemeral",
    "Number of ephemeral events sent to the AS",
    ["service"],
)

sent_todevice_counter = Counter(
    "synapse_appservice_api_sent_todevice",
    "Number of todevice messages sent to the AS",
    ["service"],
)

HOUR_IN_MS = 60 * 60 * 1000


APP_SERVICE_PREFIX = "/_matrix/app/unstable"


def _is_valid_3pe_metadata(info: JsonDict) -> bool:
    if "instances" not in info:
        return False
    if not isinstance(info["instances"], list):
        return False
    return True


def _is_valid_3pe_result(r: object, field: str) -> TypeGuard[JsonDict]:
    if not isinstance(r, dict):
        return False

    for k in (field, "protocol"):
        if k not in r:
            return False
        if not isinstance(r[k], str):
            return False

    if "fields" not in r:
        return False
    fields = r["fields"]
    if not isinstance(fields, dict):
        return False

    return True


class ApplicationServiceApi(SimpleHttpClient):
    """This class manages HS -> AS communications, including querying and
    pushing.
    """

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self.clock = hs.get_clock()

        self.protocol_meta_cache: ResponseCache[Tuple[str, str]] = ResponseCache(
            hs.get_clock(), "as_protocol_meta", timeout_ms=HOUR_IN_MS
        )

    async def query_user(self, service: "ApplicationService", user_id: str) -> bool:
        if service.url is None:
            return False

        # This is required by the configuration.
        assert service.hs_token is not None

        uri = service.url + ("/users/%s" % urllib.parse.quote(user_id))
        try:
            response = await self.get_json(
                uri,
                {"access_token": service.hs_token},
                headers={"Authorization": [f"Bearer {service.hs_token}"]},
            )
            if response is not None:  # just an empty json object
                return True
        except CodeMessageException as e:
            if e.code == 404:
                return False
            logger.warning("query_user to %s received %s", uri, e.code)
        except Exception as ex:
            logger.warning("query_user to %s threw exception %s", uri, ex)
        return False

    async def query_alias(self, service: "ApplicationService", alias: str) -> bool:
        if service.url is None:
            return False

        # This is required by the configuration.
        assert service.hs_token is not None

        uri = service.url + ("/rooms/%s" % urllib.parse.quote(alias))
        try:
            response = await self.get_json(
                uri,
                {"access_token": service.hs_token},
                headers={"Authorization": [f"Bearer {service.hs_token}"]},
            )
            if response is not None:  # just an empty json object
                return True
        except CodeMessageException as e:
            logger.warning("query_alias to %s received %s", uri, e.code)
            if e.code == 404:
                return False
        except Exception as ex:
            logger.warning("query_alias to %s threw exception %s", uri, ex)
        return False

    async def query_3pe(
        self,
        service: "ApplicationService",
        kind: str,
        protocol: str,
        fields: Dict[bytes, List[bytes]],
    ) -> List[JsonDict]:
        if kind == ThirdPartyEntityKind.USER:
            required_field = "userid"
        elif kind == ThirdPartyEntityKind.LOCATION:
            required_field = "alias"
        else:
            raise ValueError("Unrecognised 'kind' argument %r to query_3pe()", kind)
        if service.url is None:
            return []

        # This is required by the configuration.
        assert service.hs_token is not None

        uri = "%s%s/thirdparty/%s/%s" % (
            service.url,
            APP_SERVICE_PREFIX,
            kind,
            urllib.parse.quote(protocol),
        )
        try:
            args: Mapping[Any, Any] = {
                **fields,
                b"access_token": service.hs_token,
            }
            response = await self.get_json(
                uri,
                args=args,
                headers={"Authorization": [f"Bearer {service.hs_token}"]},
            )
            if not isinstance(response, list):
                logger.warning(
                    "query_3pe to %s returned an invalid response %r", uri, response
                )
                return []

            ret = []
            for r in response:
                if _is_valid_3pe_result(r, field=required_field):
                    ret.append(r)
                else:
                    logger.warning(
                        "query_3pe to %s returned an invalid result %r", uri, r
                    )

            return ret
        except Exception as ex:
            logger.warning("query_3pe to %s threw exception %s", uri, ex)
            return []

    async def get_3pe_protocol(
        self, service: "ApplicationService", protocol: str
    ) -> Optional[JsonDict]:
        if service.url is None:
            return {}

        async def _get() -> Optional[JsonDict]:
            # This is required by the configuration.
            assert service.hs_token is not None
            uri = "%s%s/thirdparty/protocol/%s" % (
                service.url,
                APP_SERVICE_PREFIX,
                urllib.parse.quote(protocol),
            )
            try:
                info = await self.get_json(
                    uri,
                    {"access_token": service.hs_token},
                    headers={"Authorization": [f"Bearer {service.hs_token}"]},
                )

                if not _is_valid_3pe_metadata(info):
                    logger.warning(
                        "query_3pe_protocol to %s did not return a valid result", uri
                    )
                    return None

                for instance in info.get("instances", []):
                    network_id = instance.get("network_id", None)
                    if network_id is not None:
                        instance["instance_id"] = ThirdPartyInstanceID(
                            service.id, network_id
                        ).to_string()

                return info
            except Exception as ex:
                logger.warning("query_3pe_protocol to %s threw exception %s", uri, ex)
                return None

        key = (service.id, protocol)
        return await self.protocol_meta_cache.wrap(key, _get)

    async def ping(self, service: "ApplicationService", txn_id: Optional[str]) -> None:
        # The caller should check that url is set
        assert service.url is not None, "ping called without URL being set"

        # This is required by the configuration.
        assert service.hs_token is not None

        await self.post_json_get_json(
            uri=service.url + "/_matrix/app/unstable/fi.mau.msc2659/ping",
            post_json={"transaction_id": txn_id},
            headers={"Authorization": [f"Bearer {service.hs_token}"]},
        )

    async def push_bulk(
        self,
        service: "ApplicationService",
        events: Sequence[EventBase],
        ephemeral: List[JsonDict],
        to_device_messages: List[JsonDict],
        one_time_keys_count: TransactionOneTimeKeysCount,
        unused_fallback_keys: TransactionUnusedFallbackKeys,
        device_list_summary: DeviceListUpdates,
        txn_id: Optional[int] = None,
    ) -> bool:
        """
        Push data to an application service.

        Args:
            service: The application service to send to.
            events: The persistent events to send.
            ephemeral: The ephemeral events to send.
            to_device_messages: The to-device messages to send.
            txn_id: An unique ID to assign to this transaction. Application services should
                deduplicate transactions received with identitical IDs.

        Returns:
            True if the task succeeded, False if it failed.
        """
        if service.url is None:
            return True

        # This is required by the configuration.
        assert service.hs_token is not None

        serialized_events = self._serialize(service, events)

        if txn_id is None:
            logger.warning(
                "push_bulk: Missing txn ID sending events to %s", service.url
            )
            txn_id = 0

        uri = service.url + ("/transactions/%s" % urllib.parse.quote(str(txn_id)))

        # Never send ephemeral events to appservices that do not support it
        body: JsonDict = {"events": serialized_events}
        if service.supports_ephemeral:
            body.update(
                {
                    # TODO: Update to stable prefixes once MSC2409 completes FCP merge.
                    "de.sorunome.msc2409.ephemeral": ephemeral,
                    "de.sorunome.msc2409.to_device": to_device_messages,
                }
            )

        # TODO: Update to stable prefixes once MSC3202 completes FCP merge
        if service.msc3202_transaction_extensions:
            if one_time_keys_count:
                body[
                    "org.matrix.msc3202.device_one_time_key_counts"
                ] = one_time_keys_count
                body[
                    "org.matrix.msc3202.device_one_time_keys_count"
                ] = one_time_keys_count
            if unused_fallback_keys:
                body[
                    "org.matrix.msc3202.device_unused_fallback_key_types"
                ] = unused_fallback_keys
            if device_list_summary:
                body["org.matrix.msc3202.device_lists"] = {
                    "changed": list(device_list_summary.changed),
                    "left": list(device_list_summary.left),
                }

        try:
            await self.put_json(
                uri=uri,
                json_body=body,
                args={"access_token": service.hs_token},
                headers={"Authorization": [f"Bearer {service.hs_token}"]},
            )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "push_bulk to %s succeeded! events=%s",
                    uri,
                    [event.get("event_id") for event in events],
                )
            sent_transactions_counter.labels(service.id).inc()
            sent_events_counter.labels(service.id).inc(len(serialized_events))
            sent_ephemeral_counter.labels(service.id).inc(len(ephemeral))
            sent_todevice_counter.labels(service.id).inc(len(to_device_messages))
            return True
        except CodeMessageException as e:
            logger.warning(
                "push_bulk to %s received code=%s msg=%s",
                uri,
                e.code,
                e.msg,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
        except Exception as ex:
            logger.warning(
                "push_bulk to %s threw exception(%s) %s args=%s",
                uri,
                type(ex).__name__,
                ex,
                ex.args,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
        failed_transactions_counter.labels(service.id).inc()
        return False

    async def claim_client_keys(
        self, service: "ApplicationService", query: List[Tuple[str, str, str]]
    ) -> Tuple[Dict[str, Dict[str, Dict[str, JsonDict]]], List[Tuple[str, str, str]]]:
        """Claim one time keys from an application service.

        Args:
            query: An iterable of tuples of (user ID, device ID, algorithm).

        Returns:
            A tuple of:
                A map of user ID -> a map device ID -> a map of key ID -> JSON dict.

                A copy of the input which has not been fulfilled because the
                appservice doesn't support this endpoint or has not returned
                data for that tuple.
        """
        if service.url is None:
            return {}, query

        # This is required by the configuration.
        assert service.hs_token is not None

        # Create the expected payload shape.
        body: Dict[str, Dict[str, List[str]]] = {}
        for user_id, device, algorithm in query:
            body.setdefault(user_id, {}).setdefault(device, []).append(algorithm)

        uri = f"{service.url}/_matrix/app/unstable/org.matrix.msc3983/keys/claim"
        try:
            response = await self.post_json_get_json(
                uri,
                body,
                headers={"Authorization": [f"Bearer {service.hs_token}"]},
            )
        except CodeMessageException as e:
            # The appservice doesn't support this endpoint.
            if e.code == 404 or e.code == 405:
                return {}, query
            logger.warning("claim_keys to %s received %s", uri, e.code)
            return {}, query
        except Exception as ex:
            logger.warning("claim_keys to %s threw exception %s", uri, ex)
            return {}, query

        # Check if the appservice fulfilled all of the queried user/device/algorithms
        # or if some are still missing.
        #
        # TODO This places a lot of faith in the response shape being correct.
        missing = [
            (user_id, device, algorithm)
            for user_id, device, algorithm in query
            if algorithm not in response.get(user_id, {}).get(device, [])
        ]

        return response, missing

    def _serialize(
        self, service: "ApplicationService", events: Iterable[EventBase]
    ) -> List[JsonDict]:
        time_now = self.clock.time_msec()
        return [
            serialize_event(
                e,
                time_now,
                config=SerializeEventConfig(
                    as_client_event=True,
                    # If this is an invite or a knock membership event, and we're interested
                    # in this user, then include any stripped state alongside the event.
                    include_stripped_room_state=(
                        e.type == EventTypes.Member
                        and (
                            e.membership == Membership.INVITE
                            or e.membership == Membership.KNOCK
                        )
                        and service.is_interested_in_user(e.state_key)
                    ),
                ),
            )
            for e in events
        ]
