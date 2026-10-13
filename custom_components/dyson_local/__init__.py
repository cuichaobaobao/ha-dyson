"""Support for Dyson devices."""

from datetime import timedelta
import logging
from typing import List, Optional

from homeassistant.config_entries import SOURCE_DISCOVERY, ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .cloud.const import CONF_AUTH, CONF_REGION, DATA_ACCOUNT, DATA_DEVICES
from .const import (
    CONF_CREDENTIAL,
    CONF_DEVICE_TYPE,
    CONF_SERIAL,
    DATA_COORDINATORS,
    DATA_DEVICES,
    DOMAIN,
)
from libdyson import (
    Dyson360Eye,
    Dyson360Heurist,
    Dyson360VisNav,
    DysonPureHotCool,
    DysonPureHotCoolLink,
    DysonPurifierHumidifyCool,
    MessageType,
    get_device,
)
from libdyson.cloud import DysonAccount, DysonAccountCN
from libdyson.dyson_device import DysonDevice
from libdyson.exceptions import (
    DysonException,
    DysonInvalidAuth,
    DysonLoginFailure,
    DysonNetworkError,
)

from .connection import async_connect_device, async_discover_host, disconnect_device

_LOGGER = logging.getLogger(__name__)

ENVIRONMENTAL_DATA_UPDATE_INTERVAL = timedelta(seconds=30)

PLATFORMS = ["camera"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Dyson integration."""
    hass.data[DOMAIN] = {
        DATA_DEVICES: {},
        DATA_COORDINATORS: {},
    }
    return True


async def async_setup_account(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a MyDyson Account."""
    _LOGGER.debug("Setting up MyDyson Account for region: %s", entry.data[CONF_REGION])

    if entry.data[CONF_REGION] == "CN":
        account = DysonAccountCN(entry.data[CONF_AUTH])
    else:
        account = DysonAccount(entry.data[CONF_AUTH])
    try:
        _LOGGER.debug("Calling account.devices() to get device list")
        devices = await hass.async_add_executor_job(account.devices)
        _LOGGER.debug("Retrieved %d devices from cloud", len(devices))
    except DysonNetworkError:
        _LOGGER.error("Cannot connect to Dyson cloud service.")
        raise ConfigEntryNotReady
    except DysonInvalidAuth:
        _LOGGER.error("Invalid authentication credentials for Dyson cloud service.")
        raise ConfigEntryNotReady
    except Exception as e:
        _LOGGER.error("Unexpected error retrieving devices: %s", str(e))
        raise ConfigEntryNotReady

    _LOGGER.debug("Starting device discovery flows for %d devices", len(devices))
    for device in devices:
        _LOGGER.debug("Creating discovery flow for device: %s (ProductType: %s)",
                      device.name, device.product_type)
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_DISCOVERY},
                data=device,
            )
        )

    hass.data[DOMAIN][entry.entry_id] = {
        DATA_ACCOUNT: account,
        DATA_DEVICES: devices,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Connect before forwarding platforms; allow HA to retry offline devices."""
    if CONF_REGION in entry.data:
        return await async_setup_account(hass, entry)

    device = get_device(
        entry.data[CONF_SERIAL],
        entry.data[CONF_CREDENTIAL],
        entry.data[CONF_DEVICE_TYPE],
    )
    if device is None:
        raise ConfigEntryNotReady("Unsupported Dyson device type")

    host = (entry.data.get(CONF_HOST) or "").strip()
    if not host:
        host = await async_discover_host(hass, device.serial)
        if not host:
            raise ConfigEntryNotReady("Dyson device not found on the local network")

    try:
        await async_connect_device(hass, device, host)
    except (DysonException, OSError) as err:
        raise ConfigEntryNotReady("Unable to connect to local Dyson device") from err

    coordinator = None
    try:
        if not isinstance(device, (Dyson360Eye, Dyson360Heurist, Dyson360VisNav)):
            async def async_update_data():
                """Poll environmental data without blocking the event loop."""
                try:
                    await hass.async_add_executor_job(device.request_environmental_data)
                except DysonException as err:
                    raise UpdateFailed("Failed to request environmental data") from err

            coordinator = DataUpdateCoordinator(
                hass,
                _LOGGER,
                name=f"environmental_{device.serial}",
                update_method=async_update_data,
                update_interval=ENVIRONMENTAL_DATA_UPDATE_INTERVAL,
                config_entry=entry,
            )

        hass.data[DOMAIN][DATA_DEVICES][entry.entry_id] = device
        hass.data[DOMAIN][DATA_COORDINATORS][entry.entry_id] = coordinator
        await hass.config_entries.async_forward_entry_setups(
            entry, _async_get_platforms(device)
        )
    except BaseException:
        hass.data[DOMAIN][DATA_DEVICES].pop(entry.entry_id, None)
        hass.data[DOMAIN][DATA_COORDINATORS].pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_shutdown()
        await hass.async_add_executor_job(disconnect_device, device)
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Release resources only after platforms have successfully unloaded."""
    if DOMAIN not in hass.data:
        return True
    if CONF_REGION in entry.data:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if unload_ok:
            hass.data[DOMAIN].pop(entry.entry_id, None)
        return unload_ok

    device = hass.data[DOMAIN][DATA_DEVICES].get(entry.entry_id)
    if device is None:
        return True
    if not await hass.config_entries.async_unload_platforms(
        entry, _async_get_platforms(device)
    ):
        return False

    coordinator = hass.data[DOMAIN][DATA_COORDINATORS].pop(entry.entry_id, None)
    try:
        if coordinator is not None:
            await coordinator.async_shutdown()
    finally:
        hass.data[DOMAIN][DATA_DEVICES].pop(entry.entry_id, None)
        await hass.async_add_executor_job(disconnect_device, device)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Let Home Assistant manage reload state and retries."""
    return await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_get_platforms(device: DysonDevice) -> List[str]:
    if (isinstance(device, Dyson360Eye)
            or isinstance(device, Dyson360Heurist)
            or isinstance(device, Dyson360VisNav)):
        return ["binary_sensor", "sensor", "vacuum"]
    platforms = ["fan", "select", "sensor", "switch"]
    if isinstance(device, DysonPureHotCool):
        platforms.append("climate")
    if isinstance(device, DysonPureHotCoolLink):
        platforms.extend(["binary_sensor", "climate"])
    if isinstance(device, DysonPurifierHumidifyCool):
        platforms.append("humidifier")
    if hasattr(device, "filter_life") or hasattr(device, "carbon_filter_life") or hasattr(device, "hepa_filter_life"):
        platforms.append("button")
    return platforms


class DysonEntity(Entity):
    """Dyson entity base class."""

    _MESSAGE_TYPE = MessageType.STATE

    def __init__(self, device: DysonDevice, name: str):
        """Initialize the entity."""
        self._device = device
        self._name = name

    async def async_added_to_hass(self) -> None:
        """Call when entity is added to hass."""
        self._device.add_message_listener(self._on_message)

    async def async_will_remove_from_hass(self) -> None:
        """Call when entity will be removed from hass."""
        try:
            self._device.remove_message_listener(self._on_message)
        except Exception as e:
            _LOGGER.debug("Error removing message listener for %s: %s", self.unique_id, e)

    def _on_message(self, message_type: MessageType) -> None:
        if self._MESSAGE_TYPE is None or message_type == self._MESSAGE_TYPE:
            self.schedule_update_ha_state()

    @property
    def should_poll(self) -> bool:
        """No polling needed."""
        return False

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        if self.sub_name is None:
            return self._name
        return f"{self._name} {self.sub_name}"

    @property
    def sub_name(self) -> Optional[str]:
        """Return sub name of the entity."""
        return None

    @property
    def unique_id(self) -> str:
        """Return the entity unique id."""
        if self.sub_unique_id is None:
            return self._device.serial
        return f"{self._device.serial}-{self.sub_unique_id}"

    @property
    def sub_unique_id(self) -> str:
        """Return the entity sub unique id."""
        return None

    @property
    def device_info(self) -> dict:
        """Return device info of the entity."""
        return {
            "identifiers": {(DOMAIN, self._device.serial)},
            "name": self._name,
            "manufacturer": "Dyson",
            "model": self._device.device_type,
        }

