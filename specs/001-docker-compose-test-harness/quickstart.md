# Quickstart: Validating the Docker-Compose Test Harness

**Feature**: [Docker-Compose Test Harness](./spec.md)
**Data model**: [data-model.md](./data-model.md) · **Contracts**: [contracts/](./contracts/)

This guide proves the feature works end-to-end. It is a validation/run guide —
implementation details live in `tasks.md` and the implementation phase.

---

## Prerequisites

- A docker-compose compatible CLI (`docker compose` v2.12+) with a running
  Docker daemon.
- Python 3.9+; install the project with test extras:
  ```bash
  pip install -e '.[test]'        # now includes testcontainers
  ```
- No ZooKeeper binary, Java, keytool, or host Kerberos tools are required
  (FR-001, FR-011) — everything runs in containers.

## Validation scenarios (map to Success Criteria)

### V1 — Provision from official image, no local ZK (SC-001, SC-003)

1. On a host with Docker + Python only, run:
   ```bash
   pytest kazoo/tests/integ/test_client.py -k "test_create or test_auth" -v
   ```
2. **Expected**: a 3-node ensemble from `zookeeper:3.9.4` starts, healthcheck
   (`ruok`) passes, tests pass, and the stack is torn down (`docker compose down`)
   at session end. `docker ps` shows no leftover harness containers.

### V2 — Version axis (SC-002)

```bash
pytest kazoo/tests/integ/test_client.py --zk-version=3.8.3 -q
pytest kazoo/tests/integ/test_client.py --zk-version=3.7.2 -q
```
**Expected**: same tests pass with a single parameter change; no test-code edits.

### V3 — Auth axes (FR-004, FR-012)

```bash
pytest kazoo/tests/integ/test_client.py --zk-auth=digest -q
pytest kazoo/tests/integ/test_client.py --zk-auth=sasl_digest -q
pytest kazoo/tests/integ/test_client.py --zk-auth=tls -q
pytest kazoo/tests/integ/test_client.py --zk-auth=sasl_gssapi -q   # TLS + GSSAPI tunnel
```
**Expected**: auth-implied client options apply automatically
([client-connection.md](./contracts/client-connection.md)); digest auth tests
(valid + invalid credentials) pass; the GSSAPI run authenticates inside the
TLS-validated tunnel.

### V4 — Feature axis + skipping (FR-005, FR-008, SC-005)

```bash
pytest kazoo/tests/integ --zk-features=ttl,reconfig -q
```
**Expected**: TTL-gated tests run; tests incompatible with the active feature set
are reported as **skipped** with an actionable reason, never failed.

### V5 — Per-test isolation & cleanup (FR-007, SC-006)

Run any test file twice in a row.
**Expected**: identical results; `zkchroot` namespaces are unique per run and
removed at teardown (no residual znodes — check via `ruok`/a fresh client).

### V6 — Failure-injection (FR-009)

Run the request-queuing / connection tests:
```bash
pytest kazoo/tests/integ/test_client.py -k "queuing or update_host_list or session_expire" -v
```
**Expected**: `zkensemble.stop("zoo1")` / `start("zoo1")` work; clients observe
SUSPENDED/LOST then reconnect; quorum is re-established.

### V7 — Logs on failure (FR-015)

Temporarily force a failing test (or run a known-failing scenario).
**Expected**: the harness dumps the ensemble container logs to the console/report,
aiding diagnosis.

### V8 — Migration completeness (FR-010)

```bash
pytest kazoo/tests/ -q   # full suite
```
**Expected**: the previously legacy-API test files (`test_election`, `test_lock`,
`test_queue`, `test_party`, `test_partitioner`, `test_lease`, `test_interrupt`,
`test_gevent_handler`, `test_sasl`) pass on the new fixtures; `git grep` for
`KazooTestCase|KazooTestHarness|ZookeeperCluster` returns nothing outside docs.

### V9 — Multiplatform (FR-011, SC-004)

Run **V1** on Linux, macOS, and Windows hosts with `docker compose`.
**Expected**: identical behavior; no platform-specific harness branches.

### V10 — Fresh environment dependency declaration (FR-014)

From a clean venv: `pip install -e '.[test]'` then **V1**.
**Expected**: install succeeds without manual package additions (`testcontainers`
is declared; Python 3.9+ required).

---

## CI matrix (per FR-017 / SC-007)

The workflow runs the tiered matrix — all supported Python versions (3.9+,
including pypy) against ZK `3.7.x`/`3.8.x`/`3.9.x`, plus auth (`digest`,
`sasl_digest`, `tls`, `sasl_gssapi`) and feature (`ttl,reconfig`) axes on the
latest Python target. No ZooKeeper download cache, no apt/Java setup steps, and
no Python 3.8 runner entry (testcontainers 4.x requires Python >= 3.9).

---

## References

- [Research decisions](./research.md) — driver, image interface, KDC, certgen,
  compose layout, dependency changes.
- [Contracts](./contracts/cli.md) — CLI/env var surface used by the commands above.
- [Data model](./data-model.md) — entities backing the fixtures.
