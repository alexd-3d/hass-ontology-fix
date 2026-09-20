import assert from "node:assert/strict";
import test from "node:test";
import neo4j from "neo4j-driver";

import {
  HARD_LIMITS,
  createResolvers,
  serializeGraphNode,
  serializeGraphRelationship,
} from "./resolvers.js";

test("initialGraph uses fixed parameterized Cypher and clamps its limit", async () => {
  const calls = [];
  const resolvers = createResolvers({
    runQuery: async (query, parameters) => {
      calls.push({ query, parameters });
      return [];
    },
  });

  const result = await resolvers.Query.initialGraph(null, { limit: 9999 });

  assert.equal(calls.length, 1);
  assert.match(calls[0].query, /\$limit/);
  assert.doesNotMatch(calls[0].query, /9999/);
  // ON-003: limit/edgeLimit are now neo4j.int() Integer objects (required so
  // Memgraph accepts them as Cypher list-slice bounds), not plain numbers.
  assert.equal(calls[0].parameters.limit.toNumber(), HARD_LIMITS.initialNodes + 1);
  assert.deepEqual(result.nodes, []);
  assert.equal(result.pageInfo.truncated, false);
  assert.match(calls[0].query, /MATCH \(n:Area\)/);
  assert.doesNotMatch(calls[0].query, /n:Device/);
  assert.match(calls[0].query, /startNode\(r\).*ha_id/s);
  assert.match(calls[0].query, /endNode\(r\).*ha_id/s);
});

test("ON-003: every Cypher list-slice/LIMIT bound is a Bolt Integer, not a JS number", async () => {
  // Regression guard: Memgraph rejects `[0..$limit]`/`LIMIT $limit` when the
  // parameter is a Bolt Float, which is what the JS neo4j-driver produces
  // for a plain JS number. Every slice/limit bound sent to runQuery must be
  // a real neo4j.int() Integer instance.
  const calls = [];
  const resolvers = createResolvers({
    runQuery: async (query, parameters) => {
      calls.push(parameters);
      return [];
    },
  });

  await resolvers.Query.initialGraph(null, { limit: 10 });
  await resolvers.Query.expandNode(null, { id: "Entity:sensor.kitchen", nodeLimit: 5, edgeLimit: 5 });
  await resolvers.Query.searchGraph(null, { term: "kitchen", limit: 5 });

  const boundParams = [
    calls[0].limit,
    calls[0].edgeLimit,
    calls[1].nodeLimit,
    calls[1].edgeLimit,
    calls[2].limit,
  ];
  for (const value of boundParams) {
    assert.ok(neo4j.isInt(value), `expected a Bolt Integer, got ${typeof value}: ${value}`);
  }
});

test("projected relationships preserve stable graph endpoints", () => {
  const relationship = serializeGraphRelationship({
    type: "HAS_DEVICE",
    source: "Area:kitchen",
    target: "Device:lamp",
    id: "primary",
    sourceClass: "home_assistant",
    properties: { source: "home_assistant" },
  });

  assert.equal(relationship.id, "HAS_DEVICE:Area:kitchen:Device:lamp:primary");
  assert.equal(relationship.source, "Area:kitchen");
  assert.equal(relationship.target, "Device:lamp");
  // ON-006: schema.graphql's SourceClass enum only accepts HOME_ASSISTANT/
  // GENERATED/INFERRED/USER - Memgraph stores the lowercase Python-side
  // spelling ("home_assistant"), so the resolver must upcase it or every
  // response carrying a relationship (expandNode, graphElement) fails
  // graphql-js's enum serialization.
  assert.equal(relationship.sourceClass, "HOME_ASSISTANT");
});

test("ON-006: relationship sourceClass is normalized to a valid SourceClass enum value", () => {
  const known = serializeGraphRelationship({
    type: "LOCATED_IN", source: "Entity:x", target: "Area:y", id: "1",
    sourceClass: "inferred", properties: {},
  });
  assert.equal(known.sourceClass, "INFERRED");

  const unknown = serializeGraphRelationship({
    type: "LOCATED_IN", source: "Entity:x", target: "Area:y", id: "2",
    sourceClass: "some_future_value", properties: {},
  });
  assert.equal(unknown.sourceClass, null);

  const missing = serializeGraphRelationship({
    type: "LOCATED_IN", source: "Entity:x", target: "Area:y", id: "3",
    properties: {},
  });
  assert.equal(missing.sourceClass, null);
});

test("expand and search reject unsafe or unbounded caller values", async () => {
  const calls = [];
  const resolvers = createResolvers({
    runQuery: async (query, parameters) => {
      calls.push({ query, parameters });
      return [];
    },
  });

  await resolvers.Query.expandNode(null, {
    id: "Entity:sensor.kitchen",
    nodeLimit: 9999,
    edgeLimit: 9999,
  });
  await resolvers.Query.searchGraph(null, { term: " kitchen ", limit: 9999 });

  assert.equal(calls[0].parameters.id, "Entity:sensor.kitchen");
  // ON-003: same Integer-object wrapping as initialGraph, see above.
  assert.equal(calls[0].parameters.nodeLimit.toNumber(), HARD_LIMITS.expandNodes + 1);
  assert.equal(calls[0].parameters.edgeLimit.toNumber(), HARD_LIMITS.expandEdges + 1);
  assert.equal(calls[1].parameters.term, "kitchen");
  assert.equal(calls[1].parameters.limit.toNumber(), HARD_LIMITS.search + 1);
  for (const call of calls) {
    assert.doesNotMatch(call.query, /\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|CALL)\b/i);
  }
});

test("safe serialization produces stable IDs and bounded redacted properties", () => {
  const node = serializeGraphNode({
    labels: ["Entity"],
    properties: {
      ha_id: "sensor.kitchen",
      name: "Kitchen Sensor",
      access_token: "must-not-leak",
      state: "x".repeat(4096),
      ...Object.fromEntries(
        Array.from({ length: 30 }, (_, index) => [`safe_${index}`, index]),
      ),
    },
  });

  assert.equal(node.id, "Entity:sensor.kitchen");
  assert.equal(node.haId, "sensor.kitchen");
  assert.equal(node.label, "Kitchen Sensor");
  assert.equal(node.properties.length, 25);
  assert.equal(node.properties.some(({ name }) => name === "access_token"), false);
  assert.ok(node.properties.every(({ displayValue }) => displayValue.length <= 2048));
});
