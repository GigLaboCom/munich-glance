"""Tests for the TRMNL device protocol handling in /api.

These cover the two regressions that made the server silently lose all device
data: the MAC arriving in the ``ID`` header (not ``X-Device-MAC``), and the
firmware's ``{"logs": [...]}`` log payload.

Assertions read the SQLite file directly rather than through the async session:
the app owns the event loop the async engine is bound to.
"""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from trmnl_server import database
from trmnl_server.config import ServerConfig
from trmnl_server.database import Base
from trmnl_server.routes import api

DEVICE_MAC = "AA:BB:CC:DD:EE:FF"
NORMALIZED_MAC = "AABBCCDDEEFF"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Point the global config at a throwaway database and create the schema."""
    config = ServerConfig(project_root=tmp_path)
    config.ensure_directories()
    monkeypatch.setattr("trmnl_server.config._config", config, raising=False)

    # The async engine is a module-level singleton; drop it so it is rebuilt
    # against this database, inside the app's own event loop.
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_session_factory", None)

    sync_engine = create_engine(f"sqlite:///{config.db_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    return config.db_path


@pytest.fixture
def client(db_path):
    """A client for the /api router only (no lifespan, no scheduler, no plugins)."""
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as test_client:
        yield test_client


def rows(db_path, table: str) -> list[sqlite3.Row]:
    """All rows of a table, oldest first."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return list(connection.execute(f"SELECT * FROM {table} ORDER BY id"))
    finally:
        connection.close()


def test_mac_from_id_header(client, db_path):
    """The stock firmware identifies itself with the ID header."""
    response = client.post(
        "/api/log",
        headers={"ID": DEVICE_MAC},
        json={"logs": [{"message": "boot", "level": "info"}]},
    )

    assert response.status_code == 200
    assert [r["mac_address"] for r in rows(db_path, "devices")] == [NORMALIZED_MAC]


def test_x_device_mac_header_still_works(client, db_path):
    """The legacy header stays supported for custom clients."""
    client.post(
        "/api/log",
        headers={"X-Device-MAC": DEVICE_MAC},
        json={"logs": [{"message": "boot"}]},
    )

    assert [r["mac_address"] for r in rows(db_path, "devices")] == [NORMALIZED_MAC]


def test_unidentified_device_is_not_registered(client, db_path):
    """No ID header must not mint a throwaway device row on every request."""
    for _ in range(3):
        client.post("/api/log", json={"logs": [{"message": "hello"}]})

    assert rows(db_path, "devices") == []
    # The log entries are still kept, under the shared UNKNOWN bucket.
    device_logs = rows(db_path, "device_logs")
    assert len(device_logs) == 3
    assert {r["device_mac"] for r in device_logs} == {api.UNKNOWN_DEVICE_MAC}


def test_terminus_log_payload_is_stored(client, db_path):
    """{"logs": [...]} is the shape the current firmware posts."""
    response = client.post(
        "/api/log",
        headers={"ID": DEVICE_MAC},
        json={
            "logs": [
                {
                    "id": 1,
                    "message": "display refresh",
                    "level": "info",
                    "source_path": "src/display.cpp",
                    "source_line": 597,
                    "firmware_version": "1.7.8",
                    "battery_voltage": 4.11,
                    "wifi_signal": -69,
                    "free_heap_size": 160656,
                }
            ]
        },
    )

    assert response.status_code == 200
    device_logs = rows(db_path, "device_logs")
    assert len(device_logs) == 1
    entry = device_logs[0]
    assert entry["device_mac"] == NORMALIZED_MAC
    assert entry["message"] == "display refresh"
    assert entry["log_type"] == "info"
    assert entry["battery_voltage"] == pytest.approx(4.11)
    assert entry["wifi_rssi"] == -69
    assert entry["firmware_version"] == "1.7.8"
    assert entry["free_heap"] == 160656


def test_battery_reading_extracted_from_log_entry(client, db_path):
    """Battery telemetry rides along with the logs - there is no battery endpoint."""
    client.post(
        "/api/log",
        headers={"ID": DEVICE_MAC},
        json={"logs": [{"message": "wake", "battery_voltage": 4.11, "wifi_signal": -69}]},
    )

    readings = rows(db_path, "battery_readings")
    assert len(readings) == 1
    assert readings[0]["device_mac"] == NORMALIZED_MAC
    assert readings[0]["voltage"] == pytest.approx(4.11)
    assert readings[0]["rssi"] == -69


def test_battery_sampling_is_throttled(client, db_path):
    """A device polls every couple of minutes; only the first sample is kept."""
    for _ in range(3):
        client.post(
            "/api/log",
            headers={"ID": DEVICE_MAC},
            json={"logs": [{"message": "wake", "battery_voltage": 4.11}]},
        )

    assert len(rows(db_path, "battery_readings")) == 1


def test_legacy_logs_array_payload_still_works(client, db_path):
    """Older firmware format must keep working."""
    client.post(
        "/api/log",
        headers={"ID": DEVICE_MAC},
        json={
            "log": {
                "logs_array": [
                    {
                        "log_message": "sleep",
                        "log_sourcefile": "main.cpp",
                        "device_status_stamp": {
                            "current_fw_version": "1.5.2",
                            "battery_voltage": 3.9,
                            "wifi_rssi_level": -55,
                        },
                    }
                ]
            }
        },
    )

    device_logs = rows(db_path, "device_logs")
    assert len(device_logs) == 1
    assert device_logs[0]["message"] == "sleep"
    assert device_logs[0]["firmware_version"] == "1.5.2"
    assert len(rows(db_path, "battery_readings")) == 1


def test_unrecognized_payload_is_logged_not_dropped_silently(client, db_path, caplog):
    """An unknown shape must be visible in the logs, with the payload."""
    with caplog.at_level("WARNING"):
        response = client.post(
            "/api/log",
            headers={"ID": DEVICE_MAC},
            json={"something": "entirely different"},
        )

    assert response.status_code == 200
    assert rows(db_path, "device_logs") == []
    messages = [record.getMessage() for record in caplog.records]
    assert any("unrecognized payload" in message for message in messages)
    assert any("entirely different" in message for message in messages)


def test_telemetry_headers_recorded(client, db_path):
    """Battery/RSSI/firmware headers are the firmware's main telemetry channel."""
    client.post(
        "/api/log",
        headers={
            "ID": DEVICE_MAC,
            "Battery-Voltage": "4.05",
            "RSSI": "-71",
            "FW-Version": "2.1.3",
            "Refresh-Rate": "1800",
        },
        json={"logs": [{"message": "poll"}]},
    )

    device = rows(db_path, "devices")[0]
    assert device["firmware_version"] == "2.1.3"
    assert device["refresh_rate"] == 1800

    reading = rows(db_path, "battery_readings")[0]
    assert reading["voltage"] == pytest.approx(4.05)
    assert reading["rssi"] == -71


def test_display_records_device_and_telemetry(client, db_path, monkeypatch):
    """/api/display is the highest-traffic endpoint and the main telemetry channel.

    No plugin is registered here, so the handler takes its error branch — the point
    is that the device is recorded before any early return.
    """
    monkeypatch.setattr(api, "get_primary_plugin_name", lambda: None)

    response = client.get(
        "/api/display",
        headers={"ID": DEVICE_MAC, "Battery-Voltage": "4.2", "RSSI": "-58"},
    )

    assert response.status_code == 200
    assert [r["mac_address"] for r in rows(db_path, "devices")] == [NORMALIZED_MAC]
    reading = rows(db_path, "battery_readings")[0]
    assert reading["voltage"] == pytest.approx(4.2)
    assert reading["rssi"] == -58


def test_display_without_id_header_registers_nothing(db_path, client, monkeypatch):
    """The old behaviour created one device row per anonymous poll."""
    monkeypatch.setattr(api, "get_primary_plugin_name", lambda: None)

    for _ in range(5):
        client.get("/api/display")

    assert rows(db_path, "devices") == []


def test_battery_endpoint_still_accepts_manual_readings(client, db_path):
    """The non-standard /api/battery endpoint stays usable for custom clients."""
    response = client.post(
        "/api/battery",
        headers={"ID": DEVICE_MAC},
        json={"voltage": 3.8, "rssi": -60},
    )

    assert response.status_code == 200
    readings = rows(db_path, "battery_readings")
    assert len(readings) == 1
    assert readings[0]["device_mac"] == NORMALIZED_MAC
    assert readings[0]["voltage"] == pytest.approx(3.8)
