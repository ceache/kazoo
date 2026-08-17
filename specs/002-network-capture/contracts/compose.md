# Contract: Capture Compose Overlay

**Feature**: [Network Capture](../spec.md) · **Research**: [research.md](../research.md)

The `docker-compose.features-capture.yml` overlay layered on the ensemble stack
when the `capture` axis value is active.

## File list

| File | Always | When |
|---|---|---|
| `docker-compose.base.yml` | ✅ | every run |
| `docker-compose.auth-<x>.yml` | | auth ≠ plain |
| **`docker-compose.features-capture.yml`** | | `capture` in `--zk-features` |

`docker_compose_config` appends the capture overlay in `kazoo/tests/integ/conftest.py`.
`docker compose config` with the active `-f` list must validate.

> **Base-file note**: the base file's `SERVER_JVMFLAGS` interpolation gains one
> slot, `${ZK_CAPTURE_JVMFLAGS}`, next to the existing
> `${ZK_FEATURES_JVMFLAGS}` and `${ZK_AUTH_JVMFLAGS}`. The harness sets it to the
> `-javaagent:` flag **only** for the tls flavor with capture active; overlays
> still never set `SERVER_JVMFLAGS` themselves (R-02/R-04).

## Service definition (contract)

Per member `zoo1`/`zoo2`/`zoo3`, the capture overlay adds one sidecar that
joins the member's network namespace:

```yaml
services:
  zoo1-capture:                         # same for zoo2-capture, zoo3-capture
    build: ./dockerfiles/capture
    network_mode: service:zoo1          # joins zoo1's netns (member netns)
    cap_add: [NET_RAW, NET_ADMIN]       # live capture on the member's eth0
    command: >
      zoo1 -i eth0 -p -s 0
      -f "tcp port 2181 or tcp port 2281"
    volumes:
      - ${ZK_WORK_DIR}/captures:/captures
```

- **Member netns**: `network_mode: service:zooN` shares the **netns-holder**
  container's namespace (`zooN` in `docker-compose.base.yml`). The holder owns
  the netns, publishes the member's client ports, and is never stopped by
  failure-injection tests — so the capture sidecar (and its tap) survives
  member restarts by construction (R-01).
- **Interface**: `eth0` inside the member's netns, tapped **non-promiscuously**
  (`-p`): the JVM listens on exactly the published ports in that same netns, so
  `eth0` carries precisely the member's client-port traffic.
- **Snaplen**: `-s 0` → full frames, no truncation (FR-005).
- **Capture filter** (BPF): clear client port 2181 and secure client port
  2281 — the client-facing ports of the member (FR-002). Quorum traffic
  (2888/3888) is deliberately *not* captured.
- **Entrypoint**: the capture Dockerfile's entrypoint consumes the member name
  as `$1` and appends `-w /captures/kazoo-client-${name}-$(date +%s%N).pcapng`
  — unique per member and per invocation (R-08). The compose `command:` must
  therefore NOT contain `-w`.
- **Output**: bind mount `${ZK_WORK_DIR}/captures` (R-05 — survives
  `down --volumes`).
- **Readiness**: long-running service without a healthcheck; compose
  `up --wait` treats it ready once up (R-06).

### What the overlay adds to the zoo services *(US2 — pending)*

For each of `zoo1-service`/`zoo2-service`/`zoo3-service` (the ZK JVM services),
on the `tls` auth flavor only (R-02/R-09):

```yaml
services:
  zoo1-service:                            # same for zoo2-service, zoo3-service
    depends_on:
      tls-secrets-agent:
        condition: service_healthy
    volumes:
      - ${ZK_WORK_DIR}/agent/extract-tls-secrets.jar:/agent/extract-tls-secrets.jar:ro
```

- The `tls-secrets-agent` service (below) provisions the keylog agent jar.
- The keystore/truststore mounts already come from the auth-tls overlay;
  the keylog agent writes its per-node log into the existing `/logs` bind
  mount (`logs/zk<id>/tls-secrets.log`), so no additional writable mount is
  needed.
- The harness merges the three per-node keylogs into
  `${ZK_WORK_DIR}/captures/tls/zk-secrets.log` at teardown (R-09).

## Runtime preflight

When `capture` is active, the session fixture builds the capture image
(`docker compose build` in the capture overlay's context,
`_build_capture_images` in `kazoo_ensemble.py`) before `up`. Build/start
failure aborts the run with an actionable message before any test executes
(R-07). *(On US2 this same preflight will also build `tls-secrets-agent`.)*

## `tls-secrets-agent` service *(US2 — pending)*

The keylog agent provisioner is a planned US2 addition to the overlay (R-10),
not yet implemented:

```yaml
services:
  tls-secrets-agent:                     # keylog agent provisioning (R-10)
    build: ./dockerfiles/tls-secrets-agent
    volumes:
      - ${ZK_WORK_DIR}/agent:/agent
    healthcheck:
      test: ["CMD-SHELL", "test -f /agent/.ready"]
      interval: 2s
      timeout: 2s
      retries: 60
```

It is a one-shot service that installs the pinned `extract-tls-secrets-5.0.0.jar`
from the in-repo image into `${ZK_WORK_DIR}/agent` and writes `.ready`. It is a
build-time dependency of the `zooN-service` JVMs (see above), not a
network/runtime dependency.

## Isolation

The overlay requires no new persistent env vars beyond the existing per-session
interpolation set; isolation follows the parent harness: per-session
`${ZK_WORK_DIR}` and `COMPOSE_PROJECT_NAME` keep concurrent runs from
colliding (R-08).

## Cross-product matrix

`capture` composes with:
- **auth flavors**: plain, digest, sasl_digest, sasl_gssapi, tls. On `tls` the
  keylog agent + decryption material are emitted (see
  [decryption.md](./decryption.md)) — **US2, pending**; on non-tls flavors the
  agent jar is still provisioned but no `-javaagent:` flag is set and no TLS
  secrets exist.
- **server features**: standard, ttl, readonly, reconfig — orthogonal (FR-007).
- **ZK versions**: no version-specific behavior (JDK JSSE provider in all
  supported versions).