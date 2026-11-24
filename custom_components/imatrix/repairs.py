"""Repair flows (spec 822 FR-24): confirm floor plan changes before they run."""

from __future__ import annotations

from typing import Any

import probatio as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant

from .floor_plan import ISSUE_PREFIX


class FloorPlanSyncFlow(RepairsFlow):
    """Shows the planned floor, area, label and device changes; runs them on confirm."""

    def __init__(self, entry_id: str) -> None:
        """Remember which config entry the plan belongs to."""
        self.entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Start at the confirmation step."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """List the actions; apply them when the user submits."""
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        manager = getattr(getattr(entry, "runtime_data", None), "floor_plan", None)
        if manager is None or manager.pending is None:
            return self.async_abort(reason="floor_plan_gone")
        if user_input is not None:
            await manager.async_apply()
            return self.async_create_entry(data={})
        lines = [a.describe() for a in manager.pending.actions[:30]]
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "count": str(len(manager.pending.actions)),
                "actions": "\n".join(f"- {line}" for line in lines),
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Create the flow for a floor plan issue."""
    if issue_id.startswith(ISSUE_PREFIX) and data and data.get("entry_id"):
        return FloorPlanSyncFlow(str(data["entry_id"]))
    raise ValueError(f"no repair flow for {issue_id}")
