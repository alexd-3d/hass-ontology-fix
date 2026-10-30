# Changelog

## 3.12.2

### Changed

- **Memgraph telemetry is now off** (`--telemetry-enabled=false`). The image default sends system information (CPU model and count, memory, OS, kernel), resource usage and aggregate graph counts to Memgraph every 8 hours, and makes the database keep per-label node counts on every write. Nothing leaves the host any more.
- **Snapshots every hour instead of every 5 minutes** (`--storage-snapshot-interval-sec=3600`). The write-ahead log stays enabled, so durability is unchanged; after a crash the database replays at most about an hour of log on start. A final snapshot is still written on a clean stop.
- **Identical property writes no longer create database history** (`--storage-delta-on-identical-property-update=false`). The Ontology integration often writes the same value again; this removes the log, garbage-collection and snapshot work for those writes.
- **Slow queries are logged** (`--log-min-duration-ms=1000`): any query taking a second or longer appears in the add-on log with a `[slow-query]` tag, which makes recurring CPU bursts attributable to a query or to internal work.

## 3.12.1

### New features

- **Authenticated internal GraphQL adapter** on port 4000 (bearer-token, internal-network only). Provides bounded, read-only graph queries for the Ontology Explorer panel without exposing Memgraph's Bolt port to browsers.
- **Supervised multi-process model**: Memgraph, GraphQL adapter, and Memgraph Lab each run as independent supervisor processes. A crash in GraphQL or Lab does not stop Memgraph or the Ontology integration.
- **Memgraph Lab via Supervisor ingress** (admin-only, Enterprise edition with verified read-only user). Lab is disabled by default and fails closed when Enterprise authorization cannot be proven.
- **Aggregate and component health probes**: `healthcheck.sh` reports degraded (not critical) when GraphQL or Lab fail, and only fails when Memgraph itself is unreachable.
- **Supervisor Discovery integration**: the add-on announces its GraphQL URL and bearer token through the Supervisor Discovery API so the Ontology integration can auto-configure without manual input.

### Process changes

- `run.sh` now generates a GraphQL bearer token at startup (`/data/graphql/token`, mode 0600) and starts Memgraph via `supervisord`.
- `supervisor.conf` now declares three programs: `memgraph` (priority 10), `graphql` (priority 20), and `lab` (priority 30, `autostart=false`).
- Ports 4000 (GraphQL) and 3000 (Lab) are **not** host-published.

## 3.12.0

- Initial release of the Memgraph add-on, wrapping `memgraph/memgraph:3.12.0`
  with persistent storage under `/data` for use with the Home Assistant
  Ontology integration.
