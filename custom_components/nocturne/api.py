"""API client wrapping the nocturne-py SDK for Home Assistant."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from nocturne_py import (
    ApiClient,
    ApiException,
    ApsSnapshot,
    ApsSnapshotApi,
    Bolus,
    BolusApi,
    CarbIntake,
    Configuration,
    CreateBolusRequest,
    CreateCarbIntakeRequest,
    CreateStateSpanRequest,
    DataOverviewApi,
    DeviceAgeApi,
    DeviceAgeInfo,
    MeterGlucose,
    MeterGlucoseApi,
    NutritionApi,
    ProfileApi,
    PumpSnapshot,
    PumpSnapshotApi,
    SensorGlucose,
    SensorGlucoseApi,
    StateSpan,
    StateSpanCategory,
    StateSpansApi,
    StatusApi,
    SummaryApi,
    SystemEvent,
    SystemEventsApi,
    TempBasal,
    TempBasalApi,
    UploaderSnapshot,
    UploaderSnapshotApi,
    UpsertMeterGlucoseRequest,
    UpsertSensorGlucoseRequest,
)
from nocturne_py.exceptions import OpenApiException
from nocturne_py.models import DailySummaryDay, ProfileSummary, V4SummaryResponse
from urllib3.exceptions import HTTPError

from .auth import TokenProvider

_LOGGER = logging.getLogger(__name__)

DAILY_SUMMARY_CACHE_SECONDS = 30 * 60

# How far back to look for the last manual bolus when none is known yet, and how many
# boluses to read per page while skipping automatic corrections.
MANUAL_BOLUS_LOOKBACK = timedelta(hours=48)
# Room for the readings of the Sugar Matrix strip window, even at a faster CGM cadence.
RECENT_GLUCOSE_PAGE_SIZE = 64
BOLUS_PAGE_SIZE = 100
# Two hours of 5-minute auto-basal doses, with room to spare.
TEMP_BASAL_PAGE_SIZE = 100

# Recent system events to scan for the newest pump alert, and recent device statuses
# to scan for one that carries CGM details.
ALERT_SCAN_COUNT = 20
DEVICE_STATUS_SCAN_COUNT = 5

# Descriptions CareLink gives the copy of an alert it reports from the pump's alarm
# snapshot; the notification copy of the same alert carries the real message.
GENERIC_ALERT_DESCRIPTIONS = frozenset({"", "ALERT", "ALARM", "MESSAGE"})


class NocturneApiClient:
    """Nocturne v4 API client using the nocturne-py SDK."""

    def __init__(
        self,
        base_url: str,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self._config = Configuration(host=base_url.rstrip("/"))
        self._token_provider = token_provider
        self._warned_missing_reports_scope = False
        self._daily_summary_cache: tuple[date, float, DailySummaryDay | None] | None = None

    async def _ensure_token(self) -> None:
        """Set the SDK token from the provider before each request."""
        if self._token_provider is not None:
            self._config.access_token = await self._token_provider()

    def _client(self) -> ApiClient:
        return ApiClient(self._config)

    async def validate_connection(self) -> bool:
        """Validate the instance URL by hitting the status endpoint."""
        try:
            await self._ensure_token()

            def _call() -> bool:
                with self._client() as client:
                    api = StatusApi(client)
                    resp = api.status_get_status()
                    return resp.status is not None

            return await asyncio.to_thread(_call)
        except (OpenApiException, HTTPError, OSError):
            return False

    async def get_latest_glucose(self) -> SensorGlucose | None:
        """Fetch the most recent sensor glucose reading."""
        await self._ensure_token()

        def _call() -> SensorGlucose | None:
            with self._client() as client:
                api = SensorGlucoseApi(client)
                resp = api.sensor_glucose_get_all(limit=1, sort="timestamp_desc")
                if resp.data:
                    return resp.data[0]
                return None

        return await asyncio.to_thread(_call)

    async def get_recent_glucose(self, since: datetime) -> list[SensorGlucose]:
        """Fetch the sensor glucose readings since ``since``, oldest first."""
        await self._ensure_token()

        def _call() -> list[SensorGlucose]:
            with self._client() as client:
                resp = SensorGlucoseApi(client).sensor_glucose_get_all(
                    var_from=since, limit=RECENT_GLUCOSE_PAGE_SIZE, sort="timestamp_desc"
                )
                return list(reversed(resp.data or []))

        return await asyncio.to_thread(_call)

    async def get_latest_aps_snapshot(self) -> ApsSnapshot | None:
        """Fetch the most recent APS snapshot."""
        await self._ensure_token()

        def _call() -> ApsSnapshot | None:
            with self._client() as client:
                api = ApsSnapshotApi(client)
                resp = api.aps_snapshot_get_all(limit=1, sort="timestamp_desc")
                if resp.data:
                    return resp.data[0]
                return None

        return await asyncio.to_thread(_call)

    async def get_latest_pump_snapshot(self) -> PumpSnapshot | None:
        """Fetch the most recent pump snapshot."""
        await self._ensure_token()

        def _call() -> PumpSnapshot | None:
            with self._client() as client:
                api = PumpSnapshotApi(client)
                resp = api.pump_snapshot_get_all(limit=1, sort="timestamp_desc")
                if resp.data:
                    return resp.data[0]
                return None

        return await asyncio.to_thread(_call)

    async def get_latest_temp_target(self) -> StateSpan | None:
        """Fetch the most recent temporary target span, running or not.

        Returns None when the token may not read temporary targets, so an entry
        authorized before treatments were in scope keeps its other sensors.
        """
        await self._ensure_token()

        def _call() -> StateSpan | None:
            with self._client() as client:
                api = StateSpansApi(client)
                resp = api.state_spans_get_state_spans(
                    category=StateSpanCategory.TEMPORARYTARGET, limit=1, sort="timestamp_desc"
                )
                if resp.data:
                    return resp.data[0]
                return None

        try:
            return await asyncio.to_thread(_call)
        except ApiException as err:
            if err.status == 403:
                _LOGGER.debug("Token may not read temporary targets: %s", err)
                return None
            raise

    async def get_summary(self) -> V4SummaryResponse | None:
        """Fetch the current summary: the IOB and COB Nocturne computes from treatments."""
        await self._ensure_token()

        def _call() -> V4SummaryResponse | None:
            with self._client() as client:
                return SummaryApi(client).summary_get_summary()

        return await asyncio.to_thread(_call)

    async def get_temp_basals_since(self, start: datetime) -> list[TempBasal]:
        """Fetch the temp basals that started at or after ``start``, newest first."""
        await self._ensure_token()

        def _call() -> list[TempBasal]:
            with self._client() as client:
                resp = TempBasalApi(client).temp_basal_get_all(
                    var_from=start, limit=TEMP_BASAL_PAGE_SIZE, sort="timestamp_desc"
                )
                return list(resp.data or [])

        return await asyncio.to_thread(_call)

    async def get_latest_manual_bolus(self, since: datetime | None = None) -> Bolus | None:
        """Fetch the newest bolus that was not an automatic correction.

        Pumps such as the 780G log each auto-correction as a bolus, which can bury the
        last manual one under hundreds of them, so this pages back to ``since``
        (48 hours ago by default) until it finds one.
        """
        await self._ensure_token()
        start = since or datetime.now(UTC) - MANUAL_BOLUS_LOOKBACK

        def _call() -> Bolus | None:
            with self._client() as client:
                api = BolusApi(client)
                offset = 0
                while True:
                    resp = api.bolus_get_all(
                        var_from=start, limit=BOLUS_PAGE_SIZE, offset=offset, sort="timestamp_desc"
                    )
                    page = resp.data or []
                    for bolus in page:
                        if not bolus.automatic:
                            return bolus
                    if len(page) < BOLUS_PAGE_SIZE:
                        return None
                    offset += BOLUS_PAGE_SIZE

        return await asyncio.to_thread(_call)

    async def get_latest_carbs(self) -> CarbIntake | None:
        """Fetch the most recent carb entry."""
        await self._ensure_token()

        def _call() -> CarbIntake | None:
            with self._client() as client:
                resp = NutritionApi(client).nutrition_get_carb_intakes(
                    limit=1, sort="timestamp_desc"
                )
                if resp.data:
                    return resp.data[0]
                return None

        return await asyncio.to_thread(_call)

    async def get_latest_pump_alert(self) -> SystemEvent | None:
        """Fetch the newest system event that carries an alert message."""
        await self._ensure_token()

        def _call() -> SystemEvent | None:
            with self._client() as client:
                events = SystemEventsApi(client).system_events_get_system_events(
                    count=ALERT_SCAN_COUNT
                )
                for event in events:
                    if (event.description or "") not in GENERIC_ALERT_DESCRIPTIONS:
                        return event
                return None

        return await asyncio.to_thread(_call)

    async def get_cgm_status(self) -> dict[str, Any] | None:
        """Fetch the CGM details of the newest device status: transmitter battery, sensor state.

        Nocturne keeps these only in the Nightscout-style device status, which no v4
        endpoint returns and the SDK therefore does not wrap, so this reads
        ``/api/v1/devicestatus`` through the SDK's own client and token. Returns the
        ``cgm`` and ``connect`` objects, or None when no recent status has them or the
        token may not read device statuses.
        """
        await self._ensure_token()

        def _call() -> dict[str, Any] | None:
            with self._client() as client:
                request = client.param_serialize(
                    "GET",
                    "/api/v1/devicestatus",
                    query_params=[("count", DEVICE_STATUS_SCAN_COUNT)],
                    auth_settings=["bearer"],
                )
                resp = client.call_api(*request)
                resp.read()
                if resp.status == 403:
                    return None
                if not 200 <= resp.status < 300:
                    raise ApiException(http_resp=resp)
                for doc in json.loads(resp.data):
                    if isinstance(doc, dict) and isinstance(doc.get("cgm"), dict):
                        return {"cgm": doc["cgm"], "connect": doc.get("connect") or {}}
                return None

        return await asyncio.to_thread(_call)

    async def get_latest_uploader_snapshot(self) -> UploaderSnapshot | None:
        """Fetch the most recent uploader (CGM phone) snapshot."""
        await self._ensure_token()

        def _call() -> UploaderSnapshot | None:
            with self._client() as client:
                api = UploaderSnapshotApi(client)
                resp = api.uploader_snapshot_get_all(limit=1, sort="timestamp_desc")
                if resp.data:
                    return resp.data[0]
                return None

        return await asyncio.to_thread(_call)

    async def get_profile_summary(self) -> ProfileSummary | None:
        """Fetch the profile summary."""
        await self._ensure_token()

        def _call() -> ProfileSummary | None:
            with self._client() as client:
                api = ProfileApi(client)
                return api.profile_get_profile_summary()

        return await asyncio.to_thread(_call)

    async def get_daily_summary(self, today: date) -> DailySummaryDay | None:
        """Fetch the daily summary (time in range, etc.) for ``today``.

        Returns None when the grant lacks ``reports.read`` (entries authorised
        before that scope was requested keep their other sensors until reauth)
        and when the server has no data for ``today``. The answer, found or not,
        is reused for 30 minutes.
        """
        cached = self._daily_summary_cache
        if (
            cached is not None
            and cached[0] == today
            and time.monotonic() - cached[1] < DAILY_SUMMARY_CACHE_SECONDS
        ):
            return cached[2]

        await self._ensure_token()

        def _call() -> DailySummaryDay | None:
            with self._client() as client:
                api = DataOverviewApi(client)
                try:
                    resp = api.data_overview_get_daily_summary(year=today.year)
                except ApiException as err:
                    if err.status == 403:
                        if not self._warned_missing_reports_scope:
                            self._warned_missing_reports_scope = True
                            _LOGGER.warning(
                                "Time in range needs the reports.read scope; "
                                "reauthenticate the Nocturne integration to enable it"
                            )
                        return None
                    raise
                today_iso = today.isoformat()
                for day in resp.days or []:
                    if day.var_date == today_iso:
                        return day
                return None

        day = await asyncio.to_thread(_call)
        self._daily_summary_cache = (today, time.monotonic(), day)
        return day

    async def get_sensor_age(self) -> DeviceAgeInfo | None:
        """Fetch sensor age information."""
        await self._ensure_token()

        def _call() -> DeviceAgeInfo | None:
            with self._client() as client:
                api = DeviceAgeApi(client)
                resp = api.device_age_get_sensor_age()
                return resp.sensor_start

        return await asyncio.to_thread(_call)

    async def create_glucose(
        self, mgdl: float, timestamp: datetime, data_source: str = ""
    ) -> SensorGlucose | None:
        """Post a sensor glucose reading."""
        await self._ensure_token()

        def _call() -> SensorGlucose | None:
            with self._client() as client:
                api = SensorGlucoseApi(client)
                req = UpsertSensorGlucoseRequest(
                    mgdl=mgdl, timestamp=timestamp, data_source=data_source
                )
                return api.sensor_glucose_create(req)

        return await asyncio.to_thread(_call)

    async def create_meter_glucose(
        self, mgdl: float, timestamp: datetime, data_source: str = ""
    ) -> MeterGlucose | None:
        """Post a fingerstick meter glucose reading."""
        await self._ensure_token()

        def _call() -> MeterGlucose | None:
            with self._client() as client:
                api = MeterGlucoseApi(client)
                req = UpsertMeterGlucoseRequest(
                    mgdl=mgdl, timestamp=timestamp, data_source=data_source
                )
                return api.meter_glucose_create(req)

        return await asyncio.to_thread(_call)

    async def create_carb_intake(
        self, request: CreateCarbIntakeRequest
    ) -> CarbIntake | None:
        """Post a carbohydrate intake record."""
        await self._ensure_token()

        def _call() -> CarbIntake | None:
            with self._client() as client:
                api = NutritionApi(client)
                return api.nutrition_create_carb_intake(request)

        return await asyncio.to_thread(_call)

    async def create_bolus(self, request: CreateBolusRequest) -> Bolus | None:
        """Post a bolus (insulin delivery) record."""
        await self._ensure_token()

        def _call() -> Bolus | None:
            with self._client() as client:
                api = BolusApi(client)
                return api.bolus_create(request)

        return await asyncio.to_thread(_call)

    async def create_state_span(
        self, request: CreateStateSpanRequest
    ) -> StateSpan | None:
        """Post a state span (activity, exercise, etc.) record."""
        await self._ensure_token()

        def _call() -> StateSpan | None:
            with self._client() as client:
                api = StateSpansApi(client)
                return api.state_spans_create_state_span(request)

        return await asyncio.to_thread(_call)
