"""Regression tests using real HA/libdyson imports and mocked network boundaries."""

import asyncio
import importlib
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from libdyson import get_device
from libdyson.exceptions import DysonConnectTimeout, DysonInvalidCredential
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components import dyson_local as integration
from custom_components.dyson_local import connection, config_flow
from custom_components.dyson_local.const import (
    DOMAIN, DATA_DEVICES, DATA_COORDINATORS,
)
from custom_components.dyson_local.device_type import resolve_device_type

SERIAL = "TEST-XX-0000000A"
HOST = "192.0.2.10"


def make_hass():
    def executor(func, *args):
        return asyncio.get_running_loop().run_in_executor(None, func, *args)

    return SimpleNamespace(
        data={DOMAIN: {DATA_DEVICES: {}, DATA_COORDINATORS: {}}},
        async_add_executor_job=executor,
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
            async_reload=AsyncMock(return_value=True),
        ),
    )


def make_entry(host=None):
    return SimpleNamespace(
        entry_id="test-entry",
        data={"serial": SERIAL, "credential": "test-only", "device_type": "664",
              "host": host},
    )


class DiscoveryTests(unittest.TestCase):
    def test_missing_and_incomplete_advertisements(self):
        listener = connection._Listener(SERIAL)
        zc = Mock()
        name = f"664_{SERIAL}.{connection.SERVICE_TYPES[0]}"
        zc.get_service_info.return_value = None
        listener.add_service(zc, connection.SERVICE_TYPES[0], name)
        self.assertFalse(listener.found.is_set())
        zc.get_service_info.return_value = Mock(parsed_addresses=Mock(return_value=[]))
        listener.update_service(zc, connection.SERVICE_TYPES[0], name)
        self.assertFalse(listener.found.is_set())
        zc.get_service_info.return_value.parsed_addresses.return_value = [HOST]
        listener.update_service(zc, connection.SERVICE_TYPES[0], name)
        self.assertEqual(listener.host, HOST)
        self.assertTrue(listener.found.is_set())
        zc.get_service_info.return_value.parsed_addresses.assert_called_with(
            connection.IPVersion.V4Only
        )

    def test_unrelated_and_malformed_services_are_ignored(self):
        listener, zc = connection._Listener(SERIAL), Mock()
        for instance in ("malformed", "664_OTHER", f"_{SERIAL}"):
            listener.add_service(
                zc, connection.SERVICE_TYPES[0],
                f"{instance}.{connection.SERVICE_TYPES[0]}",
            )
        zc.get_service_info.assert_not_called()

    def test_eye_serial_keeps_hyphens(self):
        listener, zc = connection._Listener(SERIAL), Mock()
        zc.get_service_info.return_value.parsed_addresses.return_value = [HOST]
        listener.add_service(
            zc, connection.SERVICE_TYPES[1],
            f"Dyson-{SERIAL}.{connection.SERVICE_TYPES[1]}",
        )
        self.assertEqual(listener.host, HOST)

    def test_timeout_cancels_only_our_browser(self):
        zc, browser = Mock(), Mock()
        with patch.object(connection, "ServiceBrowser", return_value=browser), patch.object(
            connection, "DISCOVERY_TIMEOUT", 0
        ):
            self.assertIsNone(connection._discover_host(zc, SERIAL))
        browser.cancel.assert_called_once()
        zc.close.assert_not_called()

    def test_success_cancels_only_our_browser(self):
        zc, browser = Mock(), Mock()
        def create(zc_arg, types, listener):
            listener.host = HOST
            listener.found.set()
            return browser
        with patch.object(connection, "ServiceBrowser", side_effect=create):
            self.assertEqual(connection._discover_host(zc, SERIAL), HOST)
        browser.cancel.assert_called_once()
        zc.close.assert_not_called()


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_connect_and_cleanup_off_loop(self):
        loop_thread = threading.get_ident()
        device = Mock()
        def check_thread(*args):
            self.assertNotEqual(threading.get_ident(), loop_thread)
        device.connect.side_effect = check_thread
        device.disconnect.side_effect = check_thread
        await connection.async_connect_device(make_hass(), device, HOST, validate_only=True)
        device.connect.assert_called_once_with(HOST)
        device.disconnect.assert_called_once()

    async def test_failed_connection_cleanup_preserves_original_error(self):
        device = Mock()
        device.connect.side_effect = DysonInvalidCredential()
        device.disconnect.side_effect = RuntimeError("no client")
        with self.assertRaises(DysonInvalidCredential):
            await connection.async_connect_device(make_hass(), device, HOST)
        device.disconnect.assert_called_once()

    async def test_cancelled_connection_cleans_late_success(self):
        device = Mock()
        started, finish = threading.Event(), threading.Event()
        def connect(host):
            started.set()
            finish.wait(5)
        device.connect.side_effect = connect
        task = asyncio.create_task(connection.async_connect_device(make_hass(), device, HOST))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        device.disconnect.assert_called_once()


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flow = config_flow.DysonLocalConfigFlow()
        self.flow.hass = make_hass()

    async def test_empty_host_discovers_without_persisting_ip(self):
        device = Mock()
        with patch.object(config_flow, "get_device", return_value=device), patch.object(
            config_flow, "async_discover_host", AsyncMock(return_value=HOST)
        ) as discover:
            data = await self.flow._async_get_entry_data(SERIAL, "test-only", "664", "Test", "")
        discover.assert_awaited_once_with(self.flow.hass, SERIAL)
        self.assertIsNone(data["host"])
        device.connect.assert_called_once_with(HOST)
        device.disconnect.assert_called_once()

    async def test_explicit_host_does_not_discover(self):
        device = Mock()
        with patch.object(config_flow, "get_device", return_value=device), patch.object(
            config_flow, "async_discover_host", AsyncMock()
        ) as discover:
            await self.flow._async_try_connect(SERIAL, "test-only", "664", HOST)
        discover.assert_not_awaited()
        device.disconnect.assert_called_once()

    async def test_discovery_timeout_is_cannot_find(self):
        with patch.object(config_flow, "get_device", return_value=Mock()), patch.object(
            config_flow, "async_discover_host", AsyncMock(return_value=None)
        ):
            with self.assertRaises(config_flow.CannotFind):
                await self.flow._async_try_connect(SERIAL, "test-only", "664")

    async def test_auth_and_network_errors_are_distinct(self):
        for error, expected in (
            (DysonInvalidCredential(), config_flow.InvalidAuth),
            (DysonConnectTimeout(), config_flow.CannotConnect),
            (OSError(), config_flow.CannotConnect),
        ):
            with self.subTest(error=type(error).__name__):
                device = Mock()
                device.connect.side_effect = error
                with patch.object(config_flow, "get_device", return_value=device):
                    with self.assertRaises(expected):
                        await self.flow._async_try_connect(SERIAL, "test-only", "664", HOST)
                device.disconnect.assert_called_once()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hass, self.entry = make_hass(), make_entry()
        self.device = get_device(SERIAL, "test-only", "664")
        self.device.connect = Mock()
        self.device.disconnect = Mock()
        self.coordinator = SimpleNamespace(async_shutdown=AsyncMock())
        self.patches = [
            patch.object(integration, "get_device", return_value=self.device),
            patch.object(integration, "async_discover_host", AsyncMock(return_value=HOST)),
            patch.object(integration, "DataUpdateCoordinator", return_value=self.coordinator),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_automatic_setup_then_unload(self):
        self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))
        self.device.connect.assert_called_once_with(HOST)
        self.hass.config_entries.async_forward_entry_setups.assert_awaited_once()
        self.assertIs(self.hass.data[DOMAIN][DATA_DEVICES][self.entry.entry_id], self.device)
        self.assertTrue(await integration.async_unload_entry(self.hass, self.entry))
        self.coordinator.async_shutdown.assert_awaited_once()
        self.device.disconnect.assert_called_once()
        self.assertEqual(self.hass.data[DOMAIN][DATA_DEVICES], {})

    async def test_timeout_requests_ha_retry_without_forwarding(self):
        integration.async_discover_host.return_value = None
        with self.assertRaises(ConfigEntryNotReady):
            await integration.async_setup_entry(self.hass, self.entry)
        self.hass.config_entries.async_forward_entry_setups.assert_not_awaited()
        self.assertEqual(self.hass.data[DOMAIN][DATA_DEVICES], {})

    async def test_failed_connect_requests_retry_and_disconnects(self):
        self.device.connect.side_effect = DysonConnectTimeout()
        with self.assertRaises(ConfigEntryNotReady):
            await integration.async_setup_entry(self.hass, self.entry)
        self.device.disconnect.assert_called_once()
        self.hass.config_entries.async_forward_entry_setups.assert_not_awaited()

    async def test_delayed_device_succeeds_on_next_retry(self):
        integration.async_discover_host.side_effect = [None, HOST]
        with self.assertRaises(ConfigEntryNotReady):
            await integration.async_setup_entry(self.hass, self.entry)
        self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))
        self.hass.config_entries.async_forward_entry_setups.assert_awaited_once()

    async def test_unload_failure_keeps_live_resources(self):
        await integration.async_setup_entry(self.hass, self.entry)
        self.hass.config_entries.async_unload_platforms.return_value = False
        self.assertFalse(await integration.async_unload_entry(self.hass, self.entry))
        self.device.disconnect.assert_not_called()
        self.coordinator.async_shutdown.assert_not_awaited()
        self.assertIn(self.entry.entry_id, self.hass.data[DOMAIN][DATA_DEVICES])

    async def test_forward_failure_releases_connection(self):
        self.hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError("setup failed")
        with self.assertRaises(RuntimeError):
            await integration.async_setup_entry(self.hass, self.entry)
        self.device.disconnect.assert_called_once()
        self.coordinator.async_shutdown.assert_awaited_once()
        self.assertEqual(self.hass.data[DOMAIN][DATA_DEVICES], {})

    async def test_reload_uses_fresh_discovery_and_no_duplicate_state(self):
        integration.async_discover_host.side_effect = [HOST, "192.0.2.20"]
        for _ in range(2):
            await integration.async_setup_entry(self.hass, self.entry)
            self.assertEqual(len(self.hass.data[DOMAIN][DATA_DEVICES]), 1)
            await integration.async_unload_entry(self.hass, self.entry)
        self.assertEqual(self.device.connect.call_args.args, ("192.0.2.20",))
        self.assertEqual(self.device.disconnect.call_count, 2)

    async def test_explicit_ip_setup_bypasses_discovery(self):
        await integration.async_setup_entry(self.hass, make_entry(HOST))
        integration.async_discover_host.assert_not_awaited()
        self.device.connect.assert_called_once_with(HOST)


class CompatibilityTests(unittest.TestCase):
    def test_big_quiet_keeps_type_topics_and_identity(self):
        info = SimpleNamespace(product_type="664", variant=None)
        self.assertEqual(resolve_device_type(info), "664")
        device = get_device(SERIAL, "test-only", "664")
        self.assertEqual(device._command_topic, f"664/{SERIAL}/command")
        self.assertEqual(device._status_topic, f"664/{SERIAL}/status/current")
        entity = integration.DysonEntity(device, "Test")
        self.assertEqual(entity.unique_id, SERIAL)
        self.assertEqual(entity.device_info["identifiers"], {(DOMAIN, SERIAL)})

    def test_explicit_variants_preserve_mqtt_topics(self):
        for product, variant, expected in (
            ("438", "M", "438M"), ("527", "K", "527K"),
            ("358", "E", "358E"), ("438M", "M", "438M"),
        ):
            with self.subTest(expected=expected):
                info = SimpleNamespace(product_type=product, variant=variant)
                resolved = resolve_device_type(info)
                self.assertEqual(resolved, expected)
                device = get_device(SERIAL, "test-only", resolved)
                self.assertEqual(device._command_topic, f"{expected}/{SERIAL}/command")

    def test_firmware_variant_and_base_type(self):
        info = SimpleNamespace(product_type="527", variant=None, version="527KPF.01.02")
        self.assertEqual(resolve_device_type(info), "527K")
        info.version = ""
        self.assertEqual(resolve_device_type(info), "527")

    def test_platforms_import_with_installed_dependencies(self):
        for platform in ("fan", "select", "sensor", "switch", "button", "climate",
                         "humidifier", "binary_sensor", "camera"):
            with self.subTest(platform=platform):
                importlib.import_module(f"custom_components.dyson_local.{platform}")

    def test_filter_capabilities_do_not_read_missing_status(self):
        device = get_device(SERIAL, "test-only", "664")
        self.assertIn("button", integration._async_get_platforms(device))

    def test_big_quiet_controls_keep_original_payloads(self):
        device = get_device(SERIAL, "test-only", "664")
        device._set_configuration = Mock()
        device.turn_on()
        device._set_configuration.assert_called_with(fpwr="ON")
        device.turn_off()
        device._set_configuration.assert_called_with(fpwr="OFF")
        device.enable_auto_mode()
        device._set_configuration.assert_called_with(auto="ON")
        device.set_tilt(50)
        device._set_configuration.assert_called_with(otal="0050", otau="0050", anct="CUST")

    def test_big_quiet_environmental_values(self):
        device = get_device(SERIAL, "test-only", "664")
        device._handle_message({
            "msg": "ENVIRONMENTAL-CURRENT-SENSOR-DATA",
            "data": {"p25r": "0012", "p10r": "0020", "co2r": "0650",
                     "hchr": "0005", "tact": "2960", "hact": "0048"},
        })
        self.assertEqual(device.particulate_matter_2_5, 12)
        self.assertEqual(device.particulate_matter_10, 20)
        self.assertEqual(device.carbon_dioxide, 650)
        self.assertEqual(device.formaldehyde, 0.005)
        self.assertEqual(device.humidity, 48)
