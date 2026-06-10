"""Button platform for Flipper Zero Sub-GHz saved files."""

import asyncio
import logging
import os
import time

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import slugify

from .const import DOMAIN, CONF_EXTERNAL_ANTENNA

_LOGGER = logging.getLogger(__name__)

# Allow up to 5 seconds for the remote entity to be registered in hass.data
# during config entry setup before giving up on creating button entities.
REMOTE_ENTITY_READY_MAX_RETRIES = 25
REMOTE_ENTITY_READY_RETRY_DELAY_SECONDS = 0.2
BUTTON_DEBOUNCE_SECONDS = 1.0


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Sub-GHz file trigger buttons for a config entry."""
    _LOGGER.info("Setting up Sub-GHz buttons for entry %s", entry.entry_id)
    remote_entity = None
    for _ in range(REMOTE_ENTITY_READY_MAX_RETRIES):
        remote_entity = hass.data.get(DOMAIN, {}).get("remote_entities", {}).get(entry.entry_id)
        if remote_entity is not None:
            break
        await asyncio.sleep(REMOTE_ENTITY_READY_RETRY_DELAY_SECONDS)

    if remote_entity is None:
        _LOGGER.warning("Cannot create Sub-GHz buttons: remote entity not ready for entry %s", entry.entry_id)
        return

    # Recreate button set on each startup/reload to avoid duplicate stale entries.
    registry = er.async_get(hass)
    existing = [
        reg_entry
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id)
        if reg_entry.domain == "button"
    ]
    for reg_entry in existing:
        registry.async_remove(reg_entry.entity_id)
    if existing:
        _LOGGER.info("Removed %d existing Sub-GHz button entities before refresh", len(existing))

    files = []
    search_roots = [
        "/ext/subghz",
        "/ext/subghz/Saved",
        "/ext/subghz_playlist",
        "/ext/apps_data/subghz",
    ]
    fallback_root = "/ext"

    for root in search_roots:
        try:
            discovered = await remote_entity.async_list_subghz_files(root)
        except Exception as e:
            _LOGGER.debug("Cannot discover Sub-GHz files in %s on %s: %s", root, remote_entity.port, e)
            continue
        if discovered:
            _LOGGER.info("Discovered %d Sub-GHz files in %s for %s", len(discovered), root, remote_entity.port)
            files.extend(discovered)

    if not files:
        try:
            discovered = await remote_entity.async_list_subghz_files(fallback_root)
        except Exception as e:
            _LOGGER.debug("Cannot discover Sub-GHz files in %s on %s: %s", fallback_root, remote_entity.port, e)
        else:
            if discovered:
                _LOGGER.info("Discovered %d Sub-GHz files in %s for %s", len(discovered), fallback_root, remote_entity.port)
                files.extend(discovered)

    files = sorted(set(files))

    if not files:
        _LOGGER.warning("No Sub-GHz .sub files found in known roots on %s", remote_entity.port)
        return

    _LOGGER.info("Discovered %d Sub-GHz files for %s", len(files), remote_entity.port)

    use_external_antenna = entry.options.get(
        CONF_EXTERNAL_ANTENNA,
        entry.data.get(CONF_EXTERNAL_ANTENNA, False),
    )
    antenna = 1 if use_external_antenna else 0

    entities = [
        FlipperSubGhzFileButton(remote_entity, path, antenna)
        for path in files
    ]
    async_add_entities(entities)


class FlipperSubGhzFileButton(ButtonEntity):
    """Button to replay one saved Sub-GHz file from Flipper storage."""

    def __init__(self, remote_entity, file_path, antenna=0):
        self._remote_entity = remote_entity
        self._port = remote_entity.port
        self._file_path = file_path
        self._antenna = antenna
        self._press_lock = asyncio.Lock()
        self._last_press_time = 0.0

        base_name = os.path.splitext(os.path.basename(file_path))[0] or "subghz"
        self._attr_name = f"Sub-GHz {base_name}"
        self._attr_unique_id = f"{DOMAIN}_{self._port}_subghz_{slugify(file_path)}"

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, self._port)},
        )

    @property
    def extra_state_attributes(self):
        return {
            "file_path": self._file_path,
            "command": f"subghz-file:path={self._file_path},repeat=1,antenna={self._antenna}",
        }

    async def async_press(self):
        """Replay file when button is pressed."""
        now = time.monotonic()
        if now - self._last_press_time < BUTTON_DEBOUNCE_SECONDS:
            _LOGGER.debug("Debouncing Sub-GHz button press for %s", self._file_path)
            return
        if self._press_lock.locked():
            _LOGGER.debug("Sub-GHz button press already in progress for %s", self._file_path)
            return
        self._last_press_time = now
        async with self._press_lock:
            _LOGGER.info("Sending Sub-GHz saved file: %s", self._file_path)
            try:
                await self._remote_entity.async_send_subghz_from_file(self._file_path, repeat=1, antenna=self._antenna)
            except Exception as e:
                _LOGGER.error("Failed to send Sub-GHz saved file %s: %s", self._file_path, e, exc_info=True)
                raise
