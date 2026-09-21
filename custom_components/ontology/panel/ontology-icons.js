const SAFE_ICON = /^mdi:[a-z0-9-]+$/;

// Icons per HA entity domain, used when no explicit icon is set
const DOMAIN_ICONS = Object.freeze({
  alarm_control_panel: "mdi:alarm-light",
  automation: "mdi:robot",
  binary_sensor: "mdi:circle-small",
  button: "mdi:gesture-tap-button",
  calendar: "mdi:calendar",
  camera: "mdi:camera",
  climate: "mdi:thermostat",
  counter: "mdi:counter",
  cover: "mdi:window-shutter",
  device_tracker: "mdi:map-marker",
  event: "mdi:calendar-check",
  fan: "mdi:fan",
  geo_location: "mdi:map-marker",
  group: "mdi:group",
  humidifier: "mdi:air-humidifier",
  image_processing: "mdi:image-filter-frames",
  input_boolean: "mdi:toggle-switch-variant-off",
  input_datetime: "mdi:calendar-clock",
  input_number: "mdi:ray-vertex",
  input_select: "mdi:format-list-bulleted",
  input_text: "mdi:form-textbox",
  lawn_mower: "mdi:robot-mower",
  light: "mdi:lightbulb",
  lock: "mdi:lock",
  media_player: "mdi:cast-connected",
  notify: "mdi:bell",
  number: "mdi:ray-vertex",
  person: "mdi:account",
  plant: "mdi:flower",
  proximity: "mdi:near-me",
  remote: "mdi:remote",
  scene: "mdi:palette",
  script: "mdi:script-text-outline",
  select: "mdi:format-list-bulleted",
  sensor: "mdi:eye",
  siren: "mdi:alarm-bell",
  sun: "mdi:white-balance-sunny",
  switch: "mdi:toggle-switch-variant",
  tag: "mdi:tag",
  timer: "mdi:timer-outline",
  todo: "mdi:clipboard-check-outline",
  update: "mdi:package-up",
  vacuum: "mdi:robot-vacuum",
  valve: "mdi:valve",
  water_heater: "mdi:water-boiler",
  weather: "mdi:weather-partly-cloudy",
  zone: "mdi:map-marker-radius",
});

const TYPE_FALLBACKS = Object.freeze({
  AREA: "mdi:sofa",
  HOME: "mdi:home-assistant",
  FLOOR: "mdi:layers-outline",
  DEVICE: "mdi:devices",
  ENTITY: "mdi:home-assistant",
  AUTOMATION: "mdi:robot",
  SCENE: "mdi:palette",
  SCRIPT: "mdi:script-text-outline",
  DASHBOARD: "mdi:view-dashboard-outline",
  DASHBOARD_CARD: "mdi:card-outline",
  SEMANTIC_TYPE: "mdi:tag-outline",
  VALIDATION_FINDING: "mdi:alert-circle-outline",
  // ON-010: per-entity semantic classification asset nodes (see resolvers.js
  // NODE_TYPES) - each of the 8 rule labels a real entity can be tagged with.
  BATTERY_POWERED_DEVICE: "mdi:battery-outline",
  ENERGY_ASSET: "mdi:flash",
  OCCUPANCY_SENSOR: "mdi:motion-sensor",
  CLIMATE_DEVICE: "mdi:thermostat",
  NETWORK_DEVICE: "mdi:lan-connect",
  SECURITY_DEVICE: "mdi:shield-home-outline",
  VEHICLE: "mdi:car",
  GAS_CYLINDER: "mdi:propane-tank-outline",
  // ON-011: Reolink camera/NVR asset nodes (manufacturer-matched).
  CAMERA: "mdi:cctv",
  OTHER: "mdi:help-circle-outline",
});

// ON-012: a low-battery/unavailable indicator, layered on top of whichever
// icon resolveOntologyIcon would otherwise pick, so the Explorer surfaces
// "this needs attention" (dead sensor, flat battery) without the user having
// to click into every node's properties.
const BATTERY_LOW_PERCENT = 20;

function propertyValue(node, name) {
  return (node.properties || []).find((property) => property.name === name)?.value;
}

// Returns "unavailable" | "battery_low" | null. Reads only properties
// graph_builder.py already mirrors onto Entity nodes (state, device_class,
// measurement_kind/status, battery_percentage) - no backend/schema change
// needed for this to work.
export function nodeAttention(node) {
  if (node.unavailable) return "unavailable";
  if (propertyValue(node, "device_class") !== "battery") return null;
  const domain = typeof node.haId === "string" ? node.haId.split(".")[0] : null;
  if (domain === "binary_sensor") {
    // A battery-class binary_sensor's "on" state IS the low-battery alert.
    return node.state === "on" ? "battery_low" : null;
  }
  if (
    propertyValue(node, "measurement_kind") === "battery" &&
    propertyValue(node, "measurement_status") === "available"
  ) {
    const percentage = propertyValue(node, "battery_percentage");
    if (typeof percentage === "number" && percentage <= BATTERY_LOW_PERCENT) {
      return "battery_low";
    }
  }
  return null;
}

const ATTENTION_ICONS = Object.freeze({
  unavailable: "mdi:access-point-network-off",
  battery_low: "mdi:battery-alert",
});

function safeIcon(value) {
  return typeof value === "string" && SAFE_ICON.test(value) ? value : null;
}

export function fallbackIconForType(type) {
  return TYPE_FALLBACKS[String(type || "OTHER").toUpperCase()] || TYPE_FALLBACKS.OTHER;
}

export function resolveOntologyIcon(node, hass) {
  // 1. Explicit icon from graph node data
  if (safeIcon(node.icon)) return node.icon;

  // 2. Icon from stored node properties
  const propertyIcon = (node.properties || []).find(({ name }) => name === "icon")?.value;
  if (safeIcon(propertyIcon)) return propertyIcon;

  // 3. HA state attribute icon (covers entities with custom icons set in UI)
  const stateIcon = hass?.states?.[node.haId]?.attributes?.icon;
  if (safeIcon(stateIcon)) return stateIcon;

  // 4. ON-012: attention indicator (dead sensor / low battery) wins over the
  // domain/area fallbacks below, but never over an explicit icon set above -
  // the point is to surface "look at this", not to hide a deliberate choice.
  const attention = nodeAttention(node);
  if (attention) return ATTENTION_ICONS[attention];

  // 5. HA area registry icon
  if (node.type === "AREA" && hass?.areas?.[node.haId]?.icon) {
    const areaIcon = safeIcon(hass.areas[node.haId].icon);
    if (areaIcon) return areaIcon;
  }

  // 6. Entity: derive icon from domain
  if (node.type === "ENTITY" && typeof node.haId === "string" && node.haId.includes(".")) {
    const domain = node.haId.split(".")[0];
    const domainIcon = DOMAIN_ICONS[domain];
    if (domainIcon) return domainIcon;
  }

  return fallbackIconForType(node.type);
}