"""TRMNL device API endpoints."""

import logging
import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from trmnl_server.config import get_config
from trmnl_server.database import get_session
from trmnl_server.models import BatteryReading, Device, DeviceLog
from trmnl_server.services.plugins import (
    get_cached_output,
    get_plugin,
    get_primary_plugin_name,
    is_cache_valid,
    run_plugin,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["api"])

# Device MAC used for readings that arrive without any identification.
UNKNOWN_DEVICE_MAC = "UNKNOWN"

# Minimum spacing between stored battery samples per device. The firmware reports
# telemetry on every poll (~2 min); without throttling battery_readings would grow by
# ~700 rows/day for data that only moves on an hourly scale.
BATTERY_SAMPLE_INTERVAL_SECONDS = 900


class DisplayResponse(BaseModel):
    """Response for /api/display endpoint."""

    status: int = 0
    image_url: str
    filename: str
    refresh_rate: int
    update_firmware: bool = False
    firmware_url: str | None = None
    reset_firmware: bool = False
    special_function: str | None = None  # sleep, identify, restart_playlist, etc.


class DeviceStatusStamp(BaseModel):
    """Device status information in log entry."""

    wifi_status: str | None = None
    wakeup_reason: str | None = None
    current_fw_version: str | None = None
    free_heap_size: int | None = None
    special_function: str | None = None
    refresh_rate: int | None = None
    battery_voltage: float | None = None
    time_since_last_sleep_start: int | None = None
    wifi_rssi_level: int | None = None

    model_config = {"extra": "allow"}


class LogEntry(BaseModel):
    """Single log entry from device."""

    log_id: int | None = None
    creation_timestamp: int | None = None
    log_message: str | None = None
    log_codeline: int | None = None
    log_sourcefile: str | None = None
    device_status_stamp: DeviceStatusStamp | None = None
    additional_info: dict | None = None

    model_config = {"extra": "allow"}


class LogArray(BaseModel):
    """Container for log entries array."""

    logs_array: list[LogEntry] = []

    model_config = {"extra": "allow"}


class TerminusLogEntry(BaseModel):
    """Single log entry in the Terminus (current firmware) format.

    Field names follow usetrmnl/terminus doc/api.adoc — the payload the stock
    firmware actually posts. Everything is optional: firmware versions differ in
    which fields they populate.
    """

    id: int | None = None
    message: str | None = None
    level: str | None = None
    source_path: str | None = None
    source_line: int | None = None
    created_at: int | None = None
    firmware_version: str | None = None
    battery_voltage: float | None = None
    wifi_signal: int | None = None
    wifi_status: str | None = None
    free_heap_size: int | None = None
    max_alloc_size: int | None = None
    wake_reason: str | None = None
    sleep_duration: int | None = None
    refresh_rate: int | None = None
    special_function: str | None = None
    retry: int | None = None

    model_config = {"extra": "allow"}


class LogRequest(BaseModel):
    """Request body for /api/log endpoint.

    Accepts three shapes, newest first:
    1. Terminus / stock firmware: {"logs": [...]}
    2. Older firmware:            {"log": {"logs_array": [...]}}
    3. Legacy flat:               {"log_type": "...", "message": "..."}
    """

    logs: list[TerminusLogEntry] | None = None
    log: LogArray | None = None

    # Legacy format support
    log_type: str | None = None
    message: str | None = None
    firmware_version: str | None = None
    wifi_rssi: int | None = None
    battery_voltage: float | None = None
    free_heap: int | None = None

    model_config = {"extra": "allow"}


class LogResponse(BaseModel):
    """Response for /api/log endpoint."""

    status: int = 0
    message: str = "ok"


class BatteryRequest(BaseModel):
    """Request body for /api/battery endpoint."""

    voltage: float
    rssi: int | None = None


class BatteryResponse(BaseModel):
    """Response for /api/battery endpoint."""

    status: int = 0
    message: str = "ok"


class SetupResponse(BaseModel):
    """Response for /api/setup endpoint."""

    status: int = 0
    api_key: str
    friendly_id: str
    message: str


def _normalize_mac(raw: str) -> str:
    """Normalize a MAC to bare uppercase hex (AA:BB:CC -> AABBCC)."""
    return raw.strip().upper().replace(":", "").replace("-", "")


def _get_device_mac(request: Request, header_mac: str | None = None) -> str | None:
    """Resolve the calling device's MAC, or None when it is unidentified.

    The stock TRMNL firmware identifies itself with the ``ID`` header (see
    usetrmnl/terminus doc/api.adoc); ``X-Device-MAC`` and ``?mac=`` are kept for
    custom clients. Returning None instead of a random ``UNKNOWN-xxxx`` id matters:
    the old fallback minted a fresh identity on every poll, so an anonymous device
    registered a new row in ``devices`` every couple of minutes.
    """
    for raw in (header_mac, request.headers.get("ID"), request.query_params.get("mac")):
        if raw and raw.strip():
            return _normalize_mac(raw)
    return None


def _header_value(request: Request, *names: str) -> str | None:
    """First non-empty value among the given (case-insensitive) header names."""
    for name in names:
        value: str | None = request.headers.get(name)
        if value and value.strip():
            return str(value.strip())
    return None


def _as_float(value: str | None) -> float | None:
    """Parse a header value as float, tolerating junk from the firmware."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _as_int(value: str | None) -> int | None:
    """Parse a header value as int, tolerating floats ("-69.0") and junk."""
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


class DeviceTelemetry(BaseModel):
    """Device state reported alongside a request.

    The TRMNL protocol has no battery endpoint — the firmware piggybacks battery,
    signal and firmware version onto the headers of every /api/display call, and
    repeats them inside /api/log entries.
    """

    voltage: float | None = None
    rssi: int | None = None
    firmware_version: str | None = None
    refresh_rate: int | None = None

    def has_reading(self) -> bool:
        """True when there is a battery sample worth storing."""
        return self.voltage is not None


def _telemetry_from_headers(request: Request) -> DeviceTelemetry:
    """Read telemetry headers, accepting both dash and underscore spellings."""
    return DeviceTelemetry(
        voltage=_as_float(_header_value(request, "Battery-Voltage", "BATTERY_VOLTAGE")),
        rssi=_as_int(_header_value(request, "RSSI", "WIFI-RSSI")),
        firmware_version=_header_value(request, "FW-Version", "FW_VERSION"),
        refresh_rate=_as_int(_header_value(request, "Refresh-Rate", "REFRESH_RATE")),
    )


async def _raw_body_preview(request: Request, limit: int = 1000) -> str:
    """Truncated raw request body, for diagnosing unrecognized payloads.

    Safe to call after FastAPI parsed the body — Starlette caches it on the request.
    """
    try:
        body = await request.body()
    except Exception as e:  # pragma: no cover - defensive
        return f"<unreadable body: {e}>"
    text = bytes(body).decode("utf-8", errors="replace")
    return text[:limit] + ("…" if len(text) > limit else "")


async def _should_sample_battery(session, mac: str) -> bool:
    """True when the last stored reading for this device is old enough."""
    result = await session.execute(
        select(BatteryReading.created_at)
        .where(BatteryReading.device_mac == mac)
        .order_by(BatteryReading.created_at.desc())
        .limit(1)
    )
    last: datetime | None = result.scalar_one_or_none()
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - last).total_seconds()
    return bool(age >= BATTERY_SAMPLE_INTERVAL_SECONDS)


async def _record_device_contact(
    mac: str | None,
    telemetry: DeviceTelemetry,
    plugin_name: str | None = None,
) -> None:
    """Upsert the device row and store a throttled battery sample.

    Unidentified callers are deliberately not registered — see _get_device_mac.
    Never raises: telemetry bookkeeping must not fail a device's display request.
    """
    if not mac:
        logger.debug("Unidentified request (no ID header), skipping device registration")
        return

    try:
        async with get_session() as session:
            result = await session.execute(select(Device).where(Device.mac_address == mac))
            device = result.scalar_one_or_none()

            if device is None:
                device = Device(mac_address=mac, current_plugin=plugin_name)
                session.add(device)
                logger.info(f"Registered new device {mac}")

            # UTC throughout: created_at/updated_at use the DB's func.now(), which is
            # UTC in SQLite. Writing a local-time last_seen skewed the two by the
            # server's UTC offset.
            device.last_seen = datetime.now(UTC)
            if plugin_name:
                device.current_plugin = plugin_name
            if telemetry.firmware_version:
                device.firmware_version = telemetry.firmware_version
            if telemetry.refresh_rate:
                device.refresh_rate = telemetry.refresh_rate

            if telemetry.has_reading() and await _should_sample_battery(session, mac):
                session.add(
                    BatteryReading(
                        device_mac=mac,
                        voltage=telemetry.voltage,
                        rssi=telemetry.rssi,
                    )
                )
                logger.info(
                    f"Battery sample from {mac}: {telemetry.voltage}V rssi={telemetry.rssi}"
                )

    except Exception as e:
        logger.warning(f"Could not record device contact for {mac}: {e}")


def _parse_time(time_str: str) -> time:
    """Parse time string (HH:MM) to time object."""
    parts = time_str.strip().split(":")
    return time(int(parts[0]), int(parts[1]))


def _is_in_sleep_window(config) -> bool:
    """Check if current time is within the sleep window.

    Handles overnight windows (e.g., 23:00 - 06:30) correctly.

    Returns:
        True if device should be sleeping, False otherwise.
    """
    if not config.sleep_enabled:
        return False

    try:
        tz = ZoneInfo(config.timezone)
        now = datetime.now(tz).time()
        sleep_start = _parse_time(config.sleep_start)
        sleep_end = _parse_time(config.sleep_end)

        # Handle overnight window (e.g., 23:00 - 06:30)
        if sleep_start > sleep_end:
            # Sleep window crosses midnight
            return now >= sleep_start or now < sleep_end
        else:
            # Normal window (e.g., 02:00 - 06:00)
            return sleep_start <= now < sleep_end

    except Exception as e:
        logger.warning(f"Error checking sleep window: {e}")
        return False


def _seconds_until_sleep_end(config) -> int:
    """Calculate seconds until sleep window ends.

    Returns:
        Seconds until sleep_end time, minimum 60 seconds.
    """
    try:
        tz = ZoneInfo(config.timezone)
        now = datetime.now(tz)
        sleep_end = _parse_time(config.sleep_end)

        # Create datetime for sleep_end today
        end_today = now.replace(
            hour=sleep_end.hour,
            minute=sleep_end.minute,
            second=0,
            microsecond=0,
        )

        # If sleep_end is before current time, it's tomorrow
        if end_today <= now:
            end_today = end_today + timedelta(days=1)

        seconds = int((end_today - now).total_seconds())

        # Minimum 60 seconds, maximum 8 hours
        return max(60, min(seconds, 28800))

    except Exception as e:
        logger.warning(f"Error calculating sleep duration: {e}")
        return 28800  # Fallback to 8 hours


def _public_base_url(request: Request) -> str:
    """Build the externally-visible base URL, honoring reverse-proxy headers.

    Traefik terminates TLS and, for the obfuscated route, strips a path prefix before
    forwarding. Without honoring X-Forwarded-Proto/Host/Prefix the app would emit
    ``http://host/...`` URLs at the root, bypassing the stripped prefix (e.g. the
    ``/daag-...`` hash) and downgrading to http. Falls back to the request URL when the
    headers are absent (direct access).
    """
    h = request.headers
    scheme = h.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = h.get("x-forwarded-host", h.get("host", request.url.netloc)).split(",")[0].strip()
    prefix = h.get("x-forwarded-prefix", "").strip().rstrip("/")
    return f"{scheme}://{host}{prefix}"


@router.get("/display", response_model=DisplayResponse)
async def get_display(
    request: Request,
    x_device_mac: str | None = Header(None, alias="X-Device-MAC"),
    plugin: str | None = Query(None, description="Specific plugin to display"),
) -> DisplayResponse:
    """Get current display image for TRMNL device.

    This is the main endpoint called by TRMNL firmware.
    Returns the URL to the current BMP image and refresh settings.

    Sleep mode: When sleep is enabled and current time is within the sleep window,
    returns special_function="sleep" to put the device into low-power sleep mode.
    """
    config = get_config()
    mac = _get_device_mac(request, x_device_mac)
    plugin_name = plugin or get_primary_plugin_name()

    # Record contact before any early return: a sleeping device still reports its
    # battery, and that is the only channel the firmware has for telemetry.
    await _record_device_contact(mac, _telemetry_from_headers(request), plugin_name=plugin_name)

    # Check if device should be sleeping
    if _is_in_sleep_window(config):
        base_url = _public_base_url(request)

        # Calculate refresh rate to wake up at sleep_end
        sleep_refresh_rate = _seconds_until_sleep_end(config)
        logger.debug(
            f"Device {mac} entering sleep mode ({config.sleep_start} - {config.sleep_end}), "
            f"wake in {sleep_refresh_rate}s ({sleep_refresh_rate // 60}min)"
        )

        # Use custom sleep image if configured, otherwise use last generated screen
        if config.sleep_image_path:
            # Custom sleep image served from /static/ directory
            image_url = f"{base_url}/static/{config.sleep_image_path}"
            filename = f"sleep_{config.sleep_image_path.replace('/', '_')}"
            logger.debug(f"Using custom sleep image: {image_url}")
        else:
            # Default: keep showing last generated screen
            image_url = f"{base_url}/generated/munichglance/screen.bmp"
            filename = "sleep.bmp"

        return DisplayResponse(
            status=0,
            image_url=image_url,
            filename=filename,
            refresh_rate=sleep_refresh_rate,
            special_function="sleep",
        )

    if not plugin_name:
        logger.error("No plugin available")
        base_url = _public_base_url(request)
        return DisplayResponse(
            status=1,
            image_url=f"{base_url}/generated/error.bmp",
            filename="error.bmp",
            refresh_rate=config.refresh_time,
        )

    # Check cache first
    if is_cache_valid(plugin_name):
        output = get_cached_output(plugin_name)
    else:
        # Run plugin to refresh
        output = await run_plugin(plugin_name)

    if not output or not output.has_image():
        logger.error(f"Plugin {plugin_name} produced no output")
        base_url = _public_base_url(request)
        return DisplayResponse(
            status=1,
            image_url=f"{base_url}/generated/error.bmp",
            filename="error.bmp",
            refresh_rate=config.refresh_time,
        )

    # Build image URL - must be a full URL for TRMNL device
    base_url = _public_base_url(request)
    if output.bmp_path:
        # Path relative to generated dir
        relative_path = output.bmp_path.relative_to(config.generated_dir)
        image_url = f"{base_url}/generated/{relative_path}?v={output.content_hash}"
        filename = f"{output.content_hash}_{output.bmp_path.name}"
    else:
        image_url = f"{base_url}/generated/screen.bmp?v={output.content_hash}"
        filename = f"{output.content_hash}_screen.bmp"

    logger.debug(f"Display response for {mac}: {image_url}")

    # Get dynamic refresh rate from plugin if available
    refresh_rate = config.refresh_time  # Default from server config
    plugin_instance = get_plugin(plugin_name)
    if plugin_instance and hasattr(plugin_instance, "get_dynamic_refresh_rate"):
        refresh_rate = plugin_instance.get_dynamic_refresh_rate()

    return DisplayResponse(
        status=0,
        image_url=image_url,
        filename=filename,
        refresh_rate=refresh_rate,
    )


@router.post("/log", response_model=LogResponse)
async def post_log(
    request: Request,
    log_data: LogRequest,
    x_device_mac: str | None = Header(None, alias="X-Device-MAC"),
) -> LogResponse:
    """Log device events.

    Called by TRMNL firmware on various events like display update,
    sleep, wake, etc.

    Accepts three formats:
    1. Terminus / stock firmware: {"logs": [...]}
    2. Older firmware format:     {"log": {"logs_array": [...]}}
    3. Legacy format:             {"log_type": "...", "message": "..."}
    """
    mac = _get_device_mac(request, x_device_mac)
    telemetry = _telemetry_from_headers(request)

    try:
        async with get_session() as session:
            # Handle Terminus format (current firmware)
            if log_data.logs:
                for terminus_entry in log_data.logs:
                    session.add(
                        DeviceLog(
                            device_mac=mac or UNKNOWN_DEVICE_MAC,
                            log_type=terminus_entry.level
                            or terminus_entry.source_path
                            or "firmware",
                            message=terminus_entry.message,
                            firmware_version=terminus_entry.firmware_version,
                            wifi_rssi=terminus_entry.wifi_signal,
                            battery_voltage=terminus_entry.battery_voltage,
                            free_heap=terminus_entry.free_heap_size,
                        )
                    )
                # Headers win when present; otherwise fall back to the newest entry
                # that carried a voltage.
                if not telemetry.has_reading():
                    for terminus_entry in reversed(log_data.logs):
                        if terminus_entry.battery_voltage is not None:
                            telemetry.voltage = terminus_entry.battery_voltage
                            telemetry.rssi = terminus_entry.wifi_signal
                            telemetry.firmware_version = terminus_entry.firmware_version
                            break
                logger.info(f"Log from {mac}: {len(log_data.logs)} entries (terminus format)")
            # Handle older TRMNL firmware format
            elif log_data.log and log_data.log.logs_array:
                for entry in log_data.log.logs_array:
                    status = entry.device_status_stamp
                    log_entry = DeviceLog(
                        device_mac=mac or UNKNOWN_DEVICE_MAC,
                        log_type=entry.log_sourcefile or "firmware",
                        message=entry.log_message,
                        firmware_version=status.current_fw_version if status else None,
                        wifi_rssi=status.wifi_rssi_level if status else None,
                        battery_voltage=status.battery_voltage if status else None,
                        free_heap=status.free_heap_size if status else None,
                    )
                    session.add(log_entry)
                    if (
                        not telemetry.has_reading()
                        and status
                        and status.battery_voltage is not None
                    ):
                        telemetry.voltage = status.battery_voltage
                        telemetry.rssi = status.wifi_rssi_level
                        telemetry.firmware_version = status.current_fw_version
                logger.info(f"Log from {mac}: {len(log_data.log.logs_array)} entries")
            # Handle legacy format
            elif log_data.log_type:
                log_entry = DeviceLog(
                    device_mac=mac or UNKNOWN_DEVICE_MAC,
                    log_type=log_data.log_type,
                    message=log_data.message,
                    firmware_version=log_data.firmware_version,
                    wifi_rssi=log_data.wifi_rssi,
                    battery_voltage=log_data.battery_voltage,
                    free_heap=log_data.free_heap,
                )
                session.add(log_entry)
                if not telemetry.has_reading() and log_data.battery_voltage is not None:
                    telemetry.voltage = log_data.battery_voltage
                    telemetry.rssi = log_data.wifi_rssi
                    telemetry.firmware_version = log_data.firmware_version
                logger.info(f"Log from {mac}: {log_data.log_type}")
            else:
                # Accept but don't store — and say so loudly with the payload, since
                # a silent drop here is exactly how this endpoint stored nothing for
                # weeks while answering 200 OK.
                logger.warning(
                    f"Log from {mac}: unrecognized payload, nothing stored: "
                    f"{await _raw_body_preview(request)}"
                )

    except Exception as e:
        logger.error(f"Failed to save log: {e}")
        return LogResponse(status=1, message=str(e))

    await _record_device_contact(mac, telemetry)

    return LogResponse(status=0, message="ok")


@router.post("/battery", response_model=BatteryResponse)
async def post_battery(
    request: Request,
    battery_data: BatteryRequest,
    x_device_mac: str | None = Header(None, alias="X-Device-MAC"),
) -> BatteryResponse:
    """Record battery and RSSI readings.

    Note: this endpoint is a MunichGlance extension, not part of the TRMNL protocol —
    stock firmware never calls it. Real devices report battery in the headers of
    /api/display and inside /api/log entries, which is where the readings come from.
    Kept for manual reporting and custom clients; writes are not throttled here.
    """
    mac = _get_device_mac(request, x_device_mac) or UNKNOWN_DEVICE_MAC

    try:
        async with get_session() as session:
            reading = BatteryReading(
                device_mac=mac,
                voltage=battery_data.voltage,
                rssi=battery_data.rssi,
            )
            session.add(reading)

        logger.info(f"Battery from {mac}: {battery_data.voltage}V")

    except Exception as e:
        logger.error(f"Failed to save battery reading: {e}")
        return BatteryResponse(status=1, message=str(e))

    return BatteryResponse(status=0, message="ok")


@router.get("/setup", response_model=SetupResponse)
async def get_setup(
    request: Request,
    x_device_mac: str | None = Header(None, alias="X-Device-MAC"),
) -> SetupResponse:
    """Get device setup information.

    Called by TRMNL firmware during initial setup.
    """
    config = get_config()
    mac = _get_device_mac(request, x_device_mac)

    # Generate or retrieve API key for device
    api_key = config.setup_api_key or f"mg_{uuid.uuid4().hex[:16]}"

    await _record_device_contact(mac, _telemetry_from_headers(request))
    logger.info(f"Setup request from {mac or 'unidentified device'}")

    return SetupResponse(
        status=0,
        api_key=api_key,
        friendly_id=config.setup_friendly_id,
        message=config.setup_message,
    )


@router.get("/health")
async def health_check() -> dict:
    """Health check endpoint."""
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "primary_plugin": get_primary_plugin_name(),
    }
