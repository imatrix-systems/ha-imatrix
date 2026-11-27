/*
 * iMatrix floor plan card (spec 822 FR-39, FR-40). Plain web component, no build step.
 *
 * type: custom:imatrix-floor-plan-card
 * config_entry_id: optional, the iMatrix entry with a floor plan (default: the first one)
 * level_id: optional, the level shown first (default: the lowest)
 * show_names: optional, show device names under the badges (default false)
 */
const ON_STATES = new Set(["on", "open", "detected", "home", "unlocked", "problem"]);
const STALE_STATES = new Set(["unavailable", "unknown"]);

/** Parse the cloud's SVG and drop anything that could run code, before it enters the page. */
function safeSvg(text) {
  const doc = new DOMParser().parseFromString(text, "image/svg+xml");
  const svg = doc.documentElement;
  if (!svg || svg.nodeName.toLowerCase() !== "svg") return null;
  svg.querySelectorAll("script, foreignObject, iframe, object, embed").forEach((n) => n.remove());
  svg.querySelectorAll("*").forEach((n) => {
    [...n.attributes].forEach((a) => {
      const name = a.name.toLowerCase();
      const value = a.value.trim().toLowerCase();
      if (name.startsWith("on")) n.removeAttribute(a.name);
      if ((name === "href" || name === "xlink:href") && !value.startsWith("#") && !value.startsWith("data:image/")) {
        n.removeAttribute(a.name);
      }
    });
  });
  svg.removeAttribute("width");
  svg.removeAttribute("height");
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  return document.importNode(svg, true);
}

class IMatrixFloorPlanCard extends HTMLElement {
  setConfig(config) {
    if (!config || typeof config !== "object") throw new Error("Invalid configuration");
    this._config = { show_names: false, ...config };
    this._levelId = config.level_id || null;
    this._data = null;
    this._error = null;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first || (!this._data && !this._loading && !this._error)) this._load();
    else this._updateBadges();
  }

  getCardSize() {
    return 8;
  }

  static getStubConfig() {
    return {};
  }

  async _load(levelId) {
    if (!this._hass) return;
    this._loading = true;
    try {
      const msg = { type: "imatrix/floor_plan/level" };
      if (this._config.config_entry_id) msg.config_entry_id = this._config.config_entry_id;
      const want = levelId || this._levelId;
      if (want) msg.level_id = want;
      this._data = await this._hass.callWS(msg);
      this._levelId = this._data.level.id;
      this._error = null;
    } catch (err) {
      this._error = (err && err.message) || "The floor plan could not be loaded.";
    } finally {
      this._loading = false;
      this._render();
    }
  }

  _stateOf(entityId) {
    return this._hass && this._hass.states[entityId];
  }

  _badgeText(marker) {
    const st = this._stateOf(marker.entities[0]);
    if (!st) return "?";
    const unit = st.attributes.unit_of_measurement;
    return unit ? `${st.state} ${unit}` : st.state;
  }

  _badgeClass(marker) {
    const st = this._stateOf(marker.entities[0]);
    if (!st || STALE_STATES.has(st.state)) return "badge stale";
    return ON_STATES.has(st.state) ? "badge on" : "badge";
  }

  _updateBadges() {
    if (!this.shadowRoot) return;
    this.shadowRoot.querySelectorAll(".badge").forEach((el) => {
      const marker = this._data && this._data.markers.find((m) => m.placementId === el.dataset.placement);
      if (!marker) return;
      el.className = this._badgeClass(marker);
      el.querySelector(".value").textContent = this._badgeText(marker);
    });
  }

  _render() {
    if (!this.shadowRoot) return;
    const root = this.shadowRoot;
    root.innerHTML = "";
    const style = document.createElement("style");
    style.textContent = `
      ha-card { padding: 12px; }
      .levels { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
      .levels button { border: 1px solid var(--divider-color); background: var(--card-background-color);
        color: var(--primary-text-color); border-radius: 16px; padding: 4px 12px; cursor: pointer; font: inherit; }
      .levels button.active { background: var(--primary-color); color: var(--text-primary-color); border-color: var(--primary-color); }
      .plan { position: relative; width: 100%; }
      .plan svg { display: block; width: 100%; height: auto; }
      .badge { position: absolute; transform: translate(-50%, -50%); display: flex; flex-direction: column;
        align-items: center; cursor: pointer; border: none; background: none; padding: 0; font: inherit; }
      .badge .value { background: var(--card-background-color); color: var(--primary-text-color);
        border: 2px solid var(--secondary-text-color); border-radius: 12px; padding: 1px 6px; font-size: 12px; white-space: nowrap; }
      .badge.on .value { border-color: var(--state-active-color, var(--primary-color)); }
      .badge.stale .value { opacity: 0.5; }
      .badge .name { font-size: 11px; color: var(--secondary-text-color); margin-top: 2px; white-space: nowrap; }
      .message { color: var(--secondary-text-color); }
    `;
    root.appendChild(style);
    const card = document.createElement("ha-card");
    root.appendChild(card);
    if (this._error) {
      const p = document.createElement("div");
      p.className = "message";
      p.textContent = this._error;
      card.appendChild(p);
      return;
    }
    if (!this._data) {
      const p = document.createElement("div");
      p.className = "message";
      p.textContent = "Loading floor plan…";
      card.appendChild(p);
      return;
    }
    const levels = document.createElement("div");
    levels.className = "levels";
    const severalBuildings = new Set(this._data.levels.map((l) => l.buildingId)).size > 1;
    this._data.levels.forEach((l) => {
      const b = document.createElement("button");
      b.textContent = severalBuildings && l.buildingName ? `${l.buildingName} ${l.name}` : l.name;
      if (l.id === this._data.level.id) b.className = "active";
      b.addEventListener("click", () => this._load(l.id));
      levels.appendChild(b);
    });
    card.appendChild(levels);
    const plan = document.createElement("div");
    plan.className = "plan";
    const svg = safeSvg(this._data.svg);
    if (svg) plan.appendChild(svg);
    this._data.markers.forEach((m) => {
      if (!m.entities.length) return;
      const badge = document.createElement("button");
      badge.className = this._badgeClass(m);
      badge.dataset.placement = m.placementId;
      badge.style.left = `${m.x}%`;
      badge.style.top = `${m.y}%`;
      badge.title = m.name;
      const value = document.createElement("span");
      value.className = "value";
      value.textContent = this._badgeText(m);
      badge.appendChild(value);
      if (this._config.show_names) {
        const name = document.createElement("span");
        name.className = "name";
        name.textContent = m.name;
        badge.appendChild(name);
      }
      badge.addEventListener("click", () => {
        this.dispatchEvent(new CustomEvent("hass-more-info", {
          detail: { entityId: m.entities[0] }, bubbles: true, composed: true,
        }));
      });
      plan.appendChild(badge);
    });
    card.appendChild(plan);
  }
}

if (!customElements.get("imatrix-floor-plan-card")) {
  customElements.define("imatrix-floor-plan-card", IMatrixFloorPlanCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "imatrix-floor-plan-card",
    name: "iMatrix floor plan",
    description: "A level of your iMatrix floor plan with live device states.",
  });
}
