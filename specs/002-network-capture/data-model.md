# Data Model: Network Capture

**Feature**: [Network Capture](../spec.md) · **Research**: [research.md](./research.md)

The feature introduces one new axis value and a small set of runtime
entities. There is no persistent state beyond the per-session capture
artifacts written under the session's temp directory.

## Entities

### ZKFeature.CAPTURE (axis value)

The new `capture` value on the `--zk-features` axis.

| Field | Value |
|---|---|
| enum member | `ZKFeature.CAPTURE` |
| CLI value | `capture` |
| env fallback | part of `ZK_FEATURES` |
| server JVM flags | **none** — deliberately absent from `FEATURE_JVM_PROPERTIES` (R-04) |

Relationships:
- Selects the `docker-compose.features-capture.yml` overlay in
  `docker_compose_config`, combined with (not replacing) the base file and any
  auth overlay.
- Combines with all auth flavors (`plain`…`tls`), all versions, and the other
  feature values (`ttl`/`readonly`/`reconfig`).

Validation rules:
- Must not alter `ZK_FEATURES_JVMFLAGS` (server behavior stays bit-identical).
- Must be selectable both standalone (`--zk-features=capture`) and combined
  (`--zk-features=ttl,capture`).

### Netns-holder container (compose service)

The pause-container-style owner of a member's network namespace.

| Field | Value |
|---|---|
| service name | `zoo1` / `zoo2` / `zoo3` |
| image | `zookeeper:${ZK_VERSION}` (reused — no new image) |
| command | `["sleep", "infinity"]` — no JVM runs here |
| role | owns the netns, publishes the member's client port (`${ZOO{N}_CLIENT_PORT}:2181`, plus `${ZOO{N}_SECURE_PORT}:2281` on tls), is the DNS name `zooN` |
| lifecycle | `restart: always`; **never stopped** by failure injection (`ZkEnsemble.stop`/`start` target `zooN-service` via `_process_service`, R-01) |

Relationships:
- Keeps the member's network namespace — and the capture sidecar's tap — alive
  across member restarts (R-01).

### Capture container (compose service)

The per-member tshark sidecar that runs for the whole session.

| Field | Value |
|---|---|
| service name | `zoo1-capture` / `zoo2-capture` / `zoo3-capture` |
| image | in-repo build `./dockerfiles/capture` (alpine:3.20 + tshark + entrypoint) |
| network | the **member's netns** via `network_mode: service:zooN`, **non-promiscuous** (`-p`) tap on the member's `eth0` (cap_add NET_RAW, NET_ADMIN) |
| capture filter | `tcp port 2181 or tcp port 2281` (BPF) |
| snaplen | `-s 0` — full-length frames (FR-005) |
| output | `${ZK_WORK_DIR}/captures/kazoo-client-zooN-<ts>.pcapng` (bind mount; `-w` appended by the entrypoint) |

Relationships:
- Observed traffic: the client-port traffic (2181 clear, 2281 secure) of **one**
  ensemble member (`zoo1`/`zoo2`/`zoo3`) on the interface the member's JVM
  listens on (R-01). The union across the three sidecars covers all three
  members; the files merge with `mergecap`.
- Lifecycle: started/stopped by session compose fixtures; joined to the holder
  netns, so member restarts do not disturb it; flushed by SIGTERM at teardown
  (R-05).

### Capture artifact (file collection)

The per-member pcapng files produced per session.

| Field | Value |
|---|---|
| path | `${ZK_WORK_DIR}/captures/kazoo-client-zooN-<ts>.pcapng` (one per member; `<ts>` = nanosecond timestamp, unique per member AND per invocation) |
| format | pcapng (tshark native) |
| retention | survives `docker compose down --volumes` (bind mount) |
| uniqueness | per-session dir (`ZK_WORK_DIR` unique per pytest session) + per-member/per-invocation names (R-08) |

Relationships:
- A collection of artifacts per capture-enabled session (SC-001); the union of
  their frames covers all three ensemble members' client ports (SC-002).

### Decryption material (files, TLS axis only)

Emitted only when `capture` is active on the `tls` auth flavor.

| Field | Value |
|---|---|
| path | `${ZK_WORK_DIR}/captures/tls/` |
| files | `zk-secrets.log`, `server-cert.pem`, `ca.pem` (see [decryption.md](./contracts/decryption.md)) |
| source | `zk-secrets.log` merged from the per-node JSSE-agent keylogs; certs copied from the existing throwaway certgen — no new credentials, **no private key exported** (R-09, FR-011) |
| confidentiality | ephemeral session secrets + throwaway certs; never logged/printed (FR-011) |

Relationships:
- Enables decryption of the TLS frames in the capture artifacts (SC-004) via the
  standard Wireshark/tshark keylog preference, covering forward-secret and
  TLS 1.3 sessions because secrets are captured on the server JVMs (R-02).

### Keylog agent (provisioned jar) *(US2 — IMPLEMENTED)*

A versioned, checksum-pinned build artifact that makes keylog capture possible.

| Field | Value |
|---|---|
| artifact | `extract-tls-secrets-5.0.0.jar` (`name.neykov:extract-tls-secrets`) |
| pin | version `5.0.0` + SHA-256 `015418eaf3ac0832909296af67fa3ec5149c53a075ead6cb29460b17db331ab0` |
| location | `${ZK_WORK_DIR}/agent/` (via the `tls-secrets-agent` sidecar, R-10) |
| license | Apache-2.0 |
| requirement | OpenJDK JSSE provider only (Conscrypt unsupported) — our stack uses JDK JSSE by default (R-02) |

Relationships:
- Mounted read-only into `zoo{1,2,3}-service`; each JVM launches with
  `-javaagent:/agent/extract-tls-secrets.jar=...` when capture is active on the
  tls flavor.
- Produces the per-node keylogs that `zk-secrets.log` is merged from.

## State transitions

```
(no capture) ──--zk-features=capture──▶ session runs ──teardown──▶ per-member artifacts retained
                                               │
                                               ├── capture start fails ──▶ abort with message (FR-010/fail-fast)
                                               ├── keylog agent fails ──▶ abort with message (R-10/R-07)   [US2]
                                               └── suite interrupted ──▶ flushed partial artifacts remain (edge case)
```

- **start → capturing**: the per-member capture sidecars (and, on US2, the
  `tls-secrets-agent`) are up before the first test (session fixture);
  `up --wait` gates readiness. Sidecars join the holder netns so they are not
  disturbed by member restarts.
- **capturing → flushed**: teardown SIGTERM → clean pcapng close; bind mount
  keeps the files (R-05). Per-node keylogs merged into `zk-secrets.log` (R-09,
  US2).
- **capturing → abort**: any compose/up failure raises pre-tests with an
  actionable message; teardown guarantee still applies (R-07).

## Validation summary

| Requirement | Entity | Check |
|---|---|---|
| FR-001/FR-007 | ZKFeature.CAPTURE | axis parity: identical results capture on/off; TLS channel never downgraded |
| FR-002/FR-004/FR-005 | Capture container/artifact | full-length, one file per member, all 3 members covered, both ports |
| FR-003/FR-009 | Artifact | survives teardown; retained after run |
| FR-006/FR-010 | Decryption material | TLS decrypts with emitted keylog material only (US2) |
| FR-008/FR-011/FR-012 | Capture container/artifact | fail-fast on start failure; ephemeral secrets + throwaway certs only; per-session + per-member uniqueness |