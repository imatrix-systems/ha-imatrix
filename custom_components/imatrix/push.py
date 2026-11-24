"""Live updates over the iMatrix stream (backend B4, spec 817 FR-H4)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pyimatrix import (
    IMatrixAuthError,
    IMatrixClient,
    IMatrixNotFoundError,
    IMatrixPermissionError,
    IMatrixStream,
)

from .const import DOMAIN, STREAM_ISSUE_AFTER

if TYPE_CHECKING:
    from . import IMatrixConfigEntry
    from .coordinator import IMatrixCoordinator

_LOGGER = logging.getLogger(__name__)


class IMatrixPush:
    """Runs the stream for one entry and feeds frames into the coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: IMatrixConfigEntry,
        coordinator: IMatrixCoordinator,
        client: IMatrixClient,
    ) -> None:
        """Create the stream with the coordinator's event cursor as `after`."""
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.state = "starting"
        self._down_since: datetime | None = dt_util.utcnow()
        self.stream = IMatrixStream(
            client.auth,
            coordinator.async_apply_frame,
            after=coordinator.event_cursor,
            on_connected=self._async_connected,
        )

    @property
    def _issue_id(self) -> str:
        return f"stream_unavailable_{self.entry.entry_id}"

    async def async_run(self) -> None:
        """Run until stopped, or until the server refuses the stream for good."""
        try:
            await self.stream.run()
        except IMatrixAuthError:
            self.state = "auth_failed"
            self.entry.async_start_reauth(self.hass)
        except IMatrixNotFoundError:
            # Server without the stream (flag off or older backend): poll only.
            self.state = "unsupported"
            _LOGGER.info("The iMatrix server has no live stream; polling instead")
        except IMatrixPermissionError:
            self.state = "forbidden"
        finally:
            self.coordinator.async_set_push(False)

    async def async_stop(self) -> None:
        """Stop the stream when the entry unloads."""
        await self.stream.stop()
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)

    async def _async_connected(self, connected: bool) -> None:
        self.coordinator.async_set_push(connected)
        if connected:
            self.state = "connected"
            self._down_since = None
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            # The stream does not replay values: re-read what changed while down.
            await self.coordinator.async_request_refresh()
        else:
            self.state = "reconnecting"
            self._down_since = dt_util.utcnow()

    @callback
    def async_check_health(self) -> None:
        """Raise a repair issue when the stream has been down for 30 minutes."""
        if (
            self.state in ("starting", "reconnecting")
            and self._down_since is not None
            and dt_util.utcnow() - self._down_since >= STREAM_ISSUE_AFTER
        ):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="stream_unavailable",
                translation_placeholders={"title": self.entry.title},
            )
