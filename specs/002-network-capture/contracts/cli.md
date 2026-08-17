# Contract: Capture Feature Axis (CLI & Environment)

**Feature**: [Network Capture](../spec.md) · **Research**: [research.md](../research.md)

## CLI option

The `capture` value extends the existing `--zk-features` option (parent
harness contract `contracts/cli.md`):

| Option | Value added | Default | Env fallback | Effect |
|---|---|---|---|---|
| `--zk-features` | …, `capture` | (unchanged) | `ZK_FEATURES` | Layers the capture overlay on the compose stack |

Examples:

```bash
# standalone capture (plain auth, standard features)
pytest kazoo/tests/integ/test_client.py --zk-features=capture -v

# capture combined with a server feature and the TLS auth axis
pytest kazoo/tests/integ/test_client.py --zk-auth=tls --zk-features=ttl,capture -v
```

## Semantics

- `capture` is **orthogonal to the server feature flags** (FR-001): it adds no
  entries to `ZK_FEATURES_JVMFLAGS`, so `SERVER_JVMFLAGS`/server behavior is
  unchanged (FR-007).
- `capture` **must not alter test outcomes, skip decisions, or client-visible
  connection behavior** (FR-007). The TLS channel is **never downgraded**: on
  the `tls` auth flavor the only addition is the *passive* keylog agent
  (`-javaagent:` via the `${ZK_CAPTURE_JVMFLAGS}` interpolation, R-02/R-04)
  which records handshake secrets without changing the negotiated ciphers or
  any observable connection behavior. Non-tls flavors get no JVM flag at all.
- Selecting `capture` with an empty/absent auth silently means `plain`, as with
  the rest of the harness.

## Environment variables consumed

| Variable | Purpose |
|---|---|
| `ZK_FEATURES` | accepted value `capture` among the comma-joined set |
| `ZK_WORK_DIR` | already exported by the harness; `${ZK_WORK_DIR}/captures` is the bind-mount target |
| `ZK_CAPTURE_JVMFLAGS` | host-computed; set to `-javaagent:/agent/extract-tls-secrets.jar=/logs/tls-secrets.log` only when `capture` is active on the `tls` flavor, else `""`; interpolated into the base `SERVER_JVMFLAGS`. The `tls-secrets-agent` service provisions the pinned/checksummed jar into `${ZK_WORK_DIR}/agent` and the zoo services `depends_on` its `.ready` healthcheck, so the flag's jar always exists on a `tls`+`capture` run (R-10, US2 implemented) |

No new user-facing environment variables are introduced by this feature.

## Markers

The existing `zk_features` marker accepts `capture` for self-check tests, e.g.
`@pytest.mark.zk_features(require=["capture"])`. No marker-system change is
needed (R-04).

## Failure semantics

- If the capture image cannot be built or the stack cannot start with `capture`
  active, the session **aborts before any test runs** with an actionable
  message (e.g. "capture: in-repo image build failed before the stack started
  ... check Docker network / registry reachability for dockerfiles/capture
  (apk tshark)") (FR-010 / R-07).
- Teardown always runs (`down --volumes` in the session fixture's `finally`),
  so a partially started stack is still cleaned up (R-07).