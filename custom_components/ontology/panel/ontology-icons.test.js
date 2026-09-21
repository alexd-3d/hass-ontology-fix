import assert from "node:assert/strict";
import test from "node:test";

import { nodeAttention, resolveOntologyIcon } from "./ontology-icons.js";

// ON-012: "needs attention" (unreachable, or a dead/low battery) is derived
// purely from properties graph_builder.py already mirrors onto the Entity
// node - these are pure-function tests, no DOM/hass instance required.

test("nodeAttention: unavailable entity takes priority over everything else", () => {
  const node = { unavailable: true, haId: "sensor.whatever", properties: [] };
  assert.equal(nodeAttention(node), "unavailable");
});

test("nodeAttention: a normal, available, non-battery entity needs no attention", () => {
  const node = {
    unavailable: false,
    haId: "light.kitchen",
    state: "on",
    properties: [{ name: "device_class", value: null }],
  };
  assert.equal(nodeAttention(node), null);
});

test("nodeAttention: sensor battery_percentage at/under the 20% threshold is low battery", () => {
  const node = {
    unavailable: false,
    haId: "sensor.pir_floor_2_battery",
    properties: [
      { name: "device_class", value: "battery" },
      { name: "measurement_kind", value: "battery" },
      { name: "measurement_status", value: "available" },
      { name: "battery_percentage", value: 18 },
    ],
  };
  assert.equal(nodeAttention(node), "battery_low");
});

test("nodeAttention: sensor battery_percentage above the threshold is fine", () => {
  const node = {
    unavailable: false,
    haId: "sensor.pir_floor_2_battery",
    properties: [
      { name: "device_class", value: "battery" },
      { name: "measurement_kind", value: "battery" },
      { name: "measurement_status", value: "available" },
      { name: "battery_percentage", value: 82 },
    ],
  };
  assert.equal(nodeAttention(node), null);
});

test("nodeAttention: an invalid/unsupported-unit battery measurement is not treated as low", () => {
  // graph_builder.py's normalize_current_measurement sets measurement_status
  // to invalid_value/unsupported_unit rather than a percentage when the
  // recorded value can't be trusted (e.g. out of 0-100 range, wrong unit) -
  // that must not be read as "battery is at 0%, must be dead".
  const node = {
    unavailable: false,
    haId: "sensor.pir_floor_2_battery",
    properties: [
      { name: "device_class", value: "battery" },
      { name: "measurement_kind", value: "battery" },
      { name: "measurement_status", value: "invalid_value" },
    ],
  };
  assert.equal(nodeAttention(node), null);
});

test("nodeAttention: binary_sensor battery device_class ON means low battery (HA convention)", () => {
  const node = {
    unavailable: false,
    haId: "binary_sensor.pir_floor_2_battery_low",
    state: "on",
    properties: [{ name: "device_class", value: "battery" }],
  };
  assert.equal(nodeAttention(node), "battery_low");
});

test("nodeAttention: binary_sensor battery device_class OFF is fine", () => {
  const node = {
    unavailable: false,
    haId: "binary_sensor.pir_floor_2_battery_low",
    state: "off",
    properties: [{ name: "device_class", value: "battery" }],
  };
  assert.equal(nodeAttention(node), null);
});

test("resolveOntologyIcon: unavailable node gets the dropped-off-network icon", () => {
  const node = { unavailable: true, type: "ENTITY", haId: "sensor.whatever", properties: [] };
  assert.equal(resolveOntologyIcon(node, null), "mdi:access-point-network-off");
});

test("resolveOntologyIcon: low-battery node gets the battery-alert icon", () => {
  const node = {
    unavailable: false,
    type: "ENTITY",
    haId: "binary_sensor.pir_floor_2_battery_low",
    state: "on",
    properties: [{ name: "device_class", value: "battery" }],
  };
  assert.equal(resolveOntologyIcon(node, null), "mdi:battery-alert");
});

test("resolveOntologyIcon: an explicit icon on the node still wins over attention", () => {
  const node = {
    unavailable: true,
    type: "ENTITY",
    haId: "sensor.whatever",
    icon: "mdi:custom-icon",
    properties: [],
  };
  assert.equal(resolveOntologyIcon(node, null), "mdi:custom-icon");
});

test("resolveOntologyIcon: a healthy entity still falls back to its domain icon", () => {
  const node = {
    unavailable: false,
    type: "ENTITY",
    haId: "light.kitchen",
    state: "on",
    properties: [],
  };
  assert.equal(resolveOntologyIcon(node, null), "mdi:lightbulb");
});
