/*
 * iMatrix dashboard strategy (spec 856 FR-25). Plain web component, no build step.
 *
 * Dashboard raw configuration:
 *   strategy:
 *     type: custom:imatrix
 *     config_entry_id: optional, one iMatrix entry (default: all)
 *     title: optional, the dashboard title (default "iMatrix")
 *
 * Home Assistant sorts a device's entities by name. This strategy shows each device's
 * sensors in the order and with the visibility set on the iMatrix Visibility page,
 * rebuilt every time the dashboard loads.
 */
class IMatrixDashboardStrategy extends HTMLElement {
  static async generate(config, hass) {
    const msg = { type: "imatrix/device_layout" };
    if (config.config_entry_id) msg.config_entry_id = config.config_entry_id;
    let devices = [];
    try {
      devices = (await hass.callWS(msg)).devices || [];
    } catch (err) {
      return IMatrixDashboardStrategy.message(config, `The iMatrix integration did not answer: ${err.message || err}`);
    }
    if (!devices.length) {
      return IMatrixDashboardStrategy.message(config, "No iMatrix device has a visible, enabled sensor.");
    }
    const cards = devices.map((d) => ({
      type: "entities",
      title: d.area ? `${d.area} · ${d.name}` : d.name,
      state_color: true,
      entities: d.entities,
    }));
    return { title: config.title || "iMatrix", views: [{ title: "iMatrix", path: "imatrix", cards }] };
  }

  static message(config, text) {
    return {
      title: config.title || "iMatrix",
      views: [{ title: "iMatrix", path: "imatrix", cards: [{ type: "markdown", content: text }] }],
    };
  }
}

if (!customElements.get("ll-strategy-dashboard-imatrix")) {
  customElements.define("ll-strategy-dashboard-imatrix", IMatrixDashboardStrategy);
}
