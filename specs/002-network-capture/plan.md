# Implementation Plan: Network Capture

**Branch**: `002-network-capture` | **Date**: 2026-08-16 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/002-network-capture/spec.md`

## Summary

Extend the docker-compose test harness's `--zk-features` axis with a new
`capture` value. When selected, **per-member tshark sidecars** (in-repo Alpine
image, mirroring the KDC pattern) run for the whole session inside each
ensemble member's **network namespace**, capturing **full-length frames**
(`-s 0`, non-promiscuous `-p` on the member's `eth0`) of the traffic on that
member's client ports (clear 2181 and, when TLS is enabled, secure 2281) into
per-member artifacts `${ZK_WORK_DIR}/captures/kazoo-client-zooN-<ts>.pcapng`
that **survive cluster teardown** via bind mount and member restarts by
construction (R-01, R-03, R-05).

The member network namespaces are owned by **netns-holder containers**
(`zoo1`/`zoo2`/`zoo3`), with the real ZooKeeper JVMs running in
`zoo1-service`/`zoo2-service`/`zoo3-service` that join the holders via
`network_mode: service:zooN` (the Kubernetes pause-container pattern). The
holder is never stopped by failure-injection tests (`ZkEnsemble.stop`/`start`
map member names to `-service` via `_process_service`), so the capture
sidecar's tap stays alive across member restarts (R-01).

On the `tls` auth flavor, the capture axis attaches the **`extract-tls-secrets`
JSSE keylog agent** to the three server JVMs (via a `${ZK_CAPTURE_JVMFLAGS}`
`-javaagent:` interpolation into the base `SERVER_JVMFLAGS`) and merges the
per-node SSLKEYLOGFILE files into `${ZK_WORK_DIR}/captures/tls/zk-secrets.log`
alongside the artifacts, so the captured TLS traffic is **decryptable using
only the emitted material** — with **default ciphers untouched** (ECDHE/TLS 1.3
kept; no downgrade, FR-007). This works because kazoo's client does not honor
`SSLKEYLOGFILE`, but the *server* is Java and keylog entries are symmetric
(R-02, R-09, R-10). **Status: US2 — planned, not yet implemented**: the
`-javaagent:` flag computation in `${ZK_CAPTURE_JVMFLAGS}` already exists, but
the `tls-secrets-agent` jar provisioning (T014/T015) and the teardown keylog
assembly (T017) are pending — a `tls`+`capture` run today would try to load a
jar that does not exist yet.

`capture` contributes **no server feature JVM flags** (it is a harness feature,
not a ZK feature); the only JVM-level addition is the passive, observational
keylog agent on the tls flavor, which never changes connection behavior or
outcomes (FR-007, R-04).

## Technical Context

**Language/Version**: Python 3.9–3.14 (CPython) + PyPy; pytest; kazoo library.
The host needs no capture tooling — everything runs in the in-repo Alpine
container.

**Primary Dependencies**:
- New: none in the Python env. The capture tool is `tshark` from Alpine's
  `community` repo (`apk add tshark`), baked into an in-repo Dockerfile
  (`kazoo/tests/integ/dockerfiles/capture/`), consistent with the harness's
  KDC pattern (R-03). For TLS decryption (US2, pending), the
  `extract-tls-secrets` Java agent (Apache-2.0, `5.0.0`, pinned + SHA-256
  verified) is downloaded at build time into an in-repo image
  (`dockerfiles/tls-secrets-agent/`) — same supply-chain discipline as `apk`
  packages, no runtime network dependency (R-10).
- Existing, unchanged: `testcontainers>=4.0,<5`, `pytest`, `attrs`, `pure_sasl`,
  `gevent`/`eventlet`. No `setup.cfg` change expected (no new Python deps).

**Storage**: N/A (test infrastructure). Capture artifacts + TLS key material
under the pytest temp dir `${ZK_WORK_DIR}/captures` via bind mount; survives
`down --volumes` (R-05).

**Testing**: pytest. Self-validation via quickstart V1–V9, plus a per-run
compose `config` validation and the existing full `kazoo/tests/integ` suite as
regression gate.

**Target Platform**: Linux, macOS, Windows — any host with a docker-compose
compatible CLI. Capture runs inside each member's container network namespace
inside the Docker VM, so there is no host-platform capture path (R-01).

**Project Type**: Python client library (Apache ZooKeeper client) + testing
infrastructure extension.

**Performance Goals**: capture adds negligible per-test overhead; the sidecars
are long-running and tap traffic already reaching each member's netns. Startup
overhead = one `docker build` of the tiny Alpine image per capture-enabled
session.

**Constraints**:
- Per-member capture collection (one tshark per member's netns, not one on the
  bridge) because capture must survive member restarts; the per-member files
  can be merged into a combined view with `mergecap` when needed.
- Full-length frames (`-s 0`), no snaplen — ZooKeeper packets carry
  options/TLV payloads that must not be truncated (FR-005).
- TLS decryption from emitted material only, including the handshake; via the
  JSSE keylog agent under capture on the tls axis, with the **TLS channel left
  at default ciphers** (no downgrade; FR-006/FR-007, R-02). *(US2 — pending)*
- In-repo, versioned capture image; the planned `tls-secrets-agent` image (no
  third-party image trust) will pin-verify the agent jar at build time
  (FR-009/FR-010, R-10). *(US2 — pending)*
- Throwaway material only; decryption uses ephemeral session secrets +
  throwaway certs — no private key exported, not logged, not committed
  (FR-011).
- No new user-facing env vars / markers; `capture` joins the existing feature
  axis (R-04), composes with auth overlays, and injects the `-javaagent:` flag
  only through the base file's `${ZK_CAPTURE_JVMFLAGS}` interpolation slot.
- Constitution V: flake8/black/mypy strict on new harness code; Angular commits.

**Scale/Scope**: one new `ZKFeature.CAPTURE` enum value; one compose overlay
(`docker-compose.features-capture.yml`) plus the netns-holder split in
`docker-compose.base.yml`; one in-repo Dockerfile (capture); a planned second
Dockerfile (tls-secrets-agent, US2); three per-member capture sidecars in the
session compose stack; decryption-material export for the tls axis (US2);
quickstart V1–V9.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design — PASS (no violations).*

| Constitution principle | Status | Evidence |
|---|---|---|
| **II. Test-first** | PASS | Feature is test-harness infrastructure; validated by quickstart V1–V9 (including failure and edge scenarios) plus the full existing integ suite as regression gate. No coverage reduction. |
| **III. Integration testing against real ZooKeeper** | PASS | Tests continue to run against the real compose-provisioned ZooKeeper cluster; capture is observational and does not alter the exercised server behavior (no feature JVM-flag changes; the tls keylog agent only records handshake secrets), and cluster logs on failure remain in place from the parent feature. |
| **IV. Backward compatibility** | PASS | Purely additive: a new `--zk-features` value, a new compose overlay, and two new Dockerfiles. No existing public API or axis value changes; non-capture runs are bit-identical (FR-007). The tls keylog agent applies only when `capture` is explicitly requested and never changes negotiated ciphers. |
| **V. Rigorous quality gates** | PASS | flake8/black/mypy strict applied to all new harness code (kazoo_ensemble additions, conftest, overlays). Angular commits. |
| **Security & Auth** | PASS | Decryption material is ephemeral session secrets + throwaway certs (no private key exported); never logged/printed/committed (FR-011). The keylog agent is passive and requires the JDK JSSE provider (the harness default). |
| **License** | PASS | Apache-2.0; the Alpine capture Dockerfile installs tshark from Alpine's official community repo (GPL-2.0-or-later Wireshark), and the keylog agent `extract-tls-secrets` is Apache-2.0 — both used solely as test tools, not vendored into the kazoo distribution. |

## Project Structure

### Documentation (this feature)

```text
specs/002-network-capture/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output — decisions R-01..R-10
├── data-model.md        # Phase 1 output — entities/fields/transitions
├── quickstart.md        # Phase 1 output — validation guide (V1–V9)
├── contracts/           # Phase 1 output
│   ├── cli.md           # `capture` axis semantics + failure semantics
│   ├── artifacts.md     # capture layout + retention + analysis
│   ├── decryption.md    # TLS decryption with emitted material only
│   └── compose.md       # capture overlay service contract
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
kazoo/
├── testing/
│   └── kazoo_ensemble.py           # KEPT + EXTENDED:
│       #  ZKFeature.CAPTURE = "capture" (R-04)
│       #  FEATURE_JVM_PROPERTIES untouched (capture contributes no feature flags)
│       #  session docker_compose fixture: build capture image + fail-fast
│       #    preflight when capture active (R-07)
│       #  ZkEnsemble.stop/start translate member names via _process_service
│       #    so failure injection targets zooN-service, never the holder (R-01)
│       #  health-wait + dump_ensemble_logs target zooN-service
│       #  TLS path (US2, pending): export ZK_CAPTURE_JVMFLAGS (the -javaagent
│       #    flag) when capture is active on the tls axis; merge per-node
│       #    keylogs into zk-secrets.log at teardown (R-02, R-09, R-10)
└── tests/
    └── integ/
        ├── conftest.py              # docker_compose_config: append
        │                            #   docker-compose.features-capture.yml
        │                            #   when `capture` in features (R-04)
        ├── docker-compose.base.yml  # netns-holder split: zooN (holder) +
        │                            #   zooN-service (ZK JVM in the holder's
        │                            #   netns); + ${ZK_CAPTURE_JVMFLAGS} in
        │                            #   SERVER_JVMFLAGS slot (R-01/R-02/R-04)
        ├── docker-compose.features-capture.yml   # three per-member
        │                            #   zooN-capture sidecars (R-01); US2
        │                            #   will add tls-secrets-agent (R-10)
        └── dockerfiles/
            ├── capture/             # in-repo Alpine tshark image (R-03)
            │   ├── Dockerfile
            │   └── capture-entrypoint.sh   # $1 = member name; unique pcapng
            └── tls-secrets-agent/   # US2 (pending): pinned, sha256-verified
                                     #   keylog agent jar (R-10)

docs/testing.rst               # + one section documenting `--zk-features=capture`
specs/001-docker-compose-test-harness/contracts/cli.md  # (optional) cross-ref note
```

**Structure Decision**: Single-project layout, mirroring the parent harness.
The capture overlay, the base-file holder/service split, and the capture
Dockerfile (plus the planned tls-secrets-agent Dockerfile) live with the
integration tests (`kazoo/tests/integ/`), exactly where the KDC/certgen infra
already sits; the axis wiring stays in `kazoo/testing/kazoo_ensemble.py`; no
new top-level packages, no `setup.cfg` changes.

## Complexity Tracking

> No Constitution Check violations to justify — this section is intentionally
> empty.