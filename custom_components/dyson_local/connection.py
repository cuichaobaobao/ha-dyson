"""Local connection helpers.

The Zeroconf instance belongs to Home Assistant. Only our browser is cancelled.
Network operations run in the executor, never on Home Assistant's event loop.
"""

import asyncio
import logging
import threading

from homeassistant.components.zeroconf import async_get_instance
from zeroconf import IPVersion, ServiceBrowser

_LOGGER = logging.getLogger(__name__)
DISCOVERY_TIMEOUT = 10
SERVICE_TYPES = ("_dyson_mqtt._tcp.local.", "_360eye_mqtt._tcp.local.")


class _Listener:
    """Resolve only the requested serial, accepting late service updates."""

    def __init__(self, serial):
        self.serial = serial
        self.host = None
        self.found = threading.Event()

    def add_service(self, zeroconf, service_type, name):
        """Handle a matching service advertisement."""
        instance = name.removesuffix("." + service_type)
        separator = "-" if service_type == SERVICE_TYPES[1] else "_"
        prefix, found_separator, serial = instance.partition(separator)
        if not found_separator or not prefix or serial != self.serial:
            return
        info = zeroconf.get_service_info(service_type, name)
        if info is None:
            return
        addresses = info.parsed_addresses(IPVersion.V4Only)
        if addresses:
            self.host = addresses[0]
            self.found.set()

    def update_service(self, zeroconf, service_type, name):
        """Retry resolution when an incomplete service gains an address."""
        self.add_service(zeroconf, service_type, name)

    def remove_service(self, zeroconf, service_type, name):
        """No persistent cache is kept by this one-shot lookup."""


def _discover_host(zeroconf, serial):
    listener = _Listener(serial)
    browser = ServiceBrowser(zeroconf, list(SERVICE_TYPES), listener)
    try:
        if listener.found.wait(DISCOVERY_TIMEOUT):
            return listener.host
        return None
    finally:
        browser.cancel()


async def async_discover_host(hass, serial):
    """Find a device without closing HA's shared Zeroconf instance."""
    zeroconf = await async_get_instance(hass)
    return await hass.async_add_executor_job(_discover_host, zeroconf, serial)


def disconnect_device(device):
    """Release MQTT resources, including partially established connections."""
    try:
        device.disconnect()
    except Exception:
        # libdyson may raise when connection failed before creating a client.
        _LOGGER.debug("Device connection cleanup failed", exc_info=True)


def _connect(device, host, validate_only):
    try:
        device.connect(host)
    except BaseException:
        disconnect_device(device)
        raise
    else:
        if validate_only:
            disconnect_device(device)


async def async_connect_device(hass, device, host, *, validate_only=False):
    """Connect off-loop; finish cleanup even if setup is cancelled."""
    job = hass.async_add_executor_job(_connect, device, host, validate_only)
    try:
        await asyncio.shield(job)
    except asyncio.CancelledError:
        # An executor job cannot be cancelled. Let it finish before disconnecting
        # so a late successful connection cannot leak after setup cancellation.
        try:
            await job
        except Exception:
            pass
        if not validate_only:
            await hass.async_add_executor_job(disconnect_device, device)
        raise
