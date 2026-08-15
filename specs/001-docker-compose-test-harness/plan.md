# Implementation Plan: Docker-Compose Test Harness

**Branch**: `001-docker-compose-test-harness` | **Date**: 2026-08-14 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-docker-compose-test-harness/spec.md`

## Summary

Modernize `kazoo.testing` so integration tests run against a ZooKeeper ensemble
provisioned via docker-compose from the **official `zookeeper` Docker Hub image**,
configured exclusively through the image's public env-var interfaces — retiring
the hand-rolled harness (`kazoo/testing/harness.py`, `kazoo/testing/common.py`)
that downloads binaries, writes `zoo.cfg`/log4j, launches Java, and generates
TLS certs in-process.

The harness drives **testcontainers-python** (`DockerCompose`, 4.x) with a
session-scoped, fixed 3-node ensemble, per-test chroot isolation, and
marker-driven skipping across the three axes (ZK version, auth scheme, feature
set). Auth flavors — plain, digest, SASL digest, SASL GSSAPI, TLS, and the
combined **TLS tunnel + GSSAPI** mode — are materialized as compose overlays
(base definition + auth overlay) backed by an in-repo Alpine KDC (ported from
`tmp/kdc/`). CI migrates to a tiered matrix (FR-017). Nine legacy-API test files
migrate to the new fixtures; the legacy public API is removed as a documented
breaking change.

## Technical Context

**Language/Version**: Python 3.9–3.14 (CPython) + PyPy; pytest; project library
is kazoo. Python 3.8 is dropped from the support matrix because the
testcontainers 4.x driver requires Python >= 3.9.

**Primary Dependencies**:
- New: `testcontainers>=4.0,<5` (compose orchestration via
  `testcontainers.compose.DockerCompose`; requires Python >= 3.9, FR-014).
- Existing in harness: `pytest`, `packaging`, `attrs`, `pure_sasl` (sasl extra),
  `gevent`/`eventlet` (handler extras).
- Removed from test extras: `pytest-docker` (superseded), `pyjks`, `pyOpenSSL`
  after `common.py` deletion (R-10).

**Storage**: N/A (test infrastructure). Container state on tmpfs volumes;
session artifacts (logs, certs, keytabs, `krb5.conf`) in the pytest temp dir
`ZK_WORK_DIR` via bind mounts.

**Testing**: pytest (the harness is exercised by `kazoo/tests/integ/` + the 9
migrated files + CI tiered matrix). Self-validation via quickstart scenarios V1–V10.

**Target Platform**: Linux, macOS, Windows — any OS with a docker-compose
compatible CLI (FR-011); CI on `ubuntu-latest` (FR-017).

**Project Type**: Python client library (Apache ZooKeeper client) + testing
infrastructure modernization.

**Performance Goals**: session startup (provision + healthcheck) within ~60s
warm / ~120s cold-excluding-pull; per-test timeout 180s (existing); cluster
reused for the whole session (amortized startup).

**Constraints**:
- Configure ZooKeeper only via official image interfaces (FR-002); no local
  ZooKeeper/Java/openssl/krb5 on the host (FR-001/011).
- No platform-specific code paths (FR-011); ephemeral host ports (edge cases).
- Throwaway credentials only; nothing sensitive committed/logged (FR-013).
- Compose layout is the **base + auth overlay** model (R-09): `SERVER_JVMFLAGS`
  is defined only in the base file via `${ZK_FEATURES_JVMFLAGS} ${ZK_AUTH_JVMFLAGS}`
  interpolation (avoids overlay `environment`-map merge overrides); readiness
  via `docker compose up --wait` (Compose v2.12+).
- Constitution V: flake8/black/mypy strict must pass; Angular commits.

**Scale/Scope**: 3-node ensemble (FR-016) × 5 auth flavors × 4 feature sets × 3
ZK version series; 6 already-migrated integ files + 9 legacy files to migrate
(FR-010); CI matrix rewrite (FR-017).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design — PASS (no violations).*

| Constitution principle | Status | Evidence |
|---|---|---|
| **II. Test-first** | PASS | All changes are test-harness migration; acceptance validated by quickstart V1–V8 and the full suite run (V8). No coverage reduction (FR-010). |
| **III. Integration testing against real ZooKeeper** | PASS | Tests still run against a real ZooKeeper cluster (containers); healthchecked before tests (FR-006); cluster logs surfaced on failure (FR-015); `KazooTestHarness`/`KazooTestCase` invocation path is replaced per the feature's explicit mandate (this feature intentionally supersedes the `ZOOKEEPER_PATH` mechanism). |

> **Governance note**: Constitution principle III documents the
> `ZOOKEEPER_PATH`/`ensure-zookeeper-env.sh` mechanism, which this feature
> intentionally replaces with the docker-compose path. Per the constitution's
> Governance section, this mechanism change SHOULD be recorded as a documented
> amendment (MINOR) to the constitution when the change lands.
| **IV. Backward compatibility** | PASS (with documented breaking change) | Legacy public API (`KazooTestCase`, `KazooTestHarness`) removed outright per clarification Q1; MUST be recorded in `CHANGES.md` under `BREAKING CHANGES` (FR-010). Python 3.8 is dropped from the support matrix (testcontainers 4.x requires Python >= 3.9 — plan-refinement clarification); all other supported Python/handler backends preserved. |
| **V. Rigorous quality gates** | PASS | flake8/black/mypy strict on all new harness code; the 9 migrated test files keep passing under all handlers (threading/gevent/eventlet). |
| **Security & Auth** | PASS | Throwaway credentials/keytabs/certs (FR-013); KDC built in-repo on Alpine (FR-018) — no third-party trust root; GSSAPI-in-TLS supported (FR-012). |
| **License** | PASS | Apache-2.0; no new vendored code — the KDC Dockerfile/entrypoint is ported from the repo's own `tmp/kdc/` (already Apache-2.0 project code). |

## Project Structure

### Documentation (this feature)

```text
specs/001-docker-compose-test-harness/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output — decisions R-01..R-12
├── data-model.md        # Phase 1 output — entities/fields/relationships
├── quickstart.md        # Phase 1 output — validation guide (V1–V10)
├── contracts/           # Phase 1 output
│   ├── cli.md           # --zk-* flags + env vars
│   ├── fixtures.md      # docker_env/zkensemble/zkclient/... contracts
│   ├── markers.md       # skip_if_zk_version / zk_* marker surface
│   ├── compose.md       # official-image interface + compose layout
│   └── client-connection.md  # auth axis → KazooClient options
└── tasks.md             # Phase 2 output (/speckit.tasks - NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
kazoo/
├── testing/
│   ├── __init__.py             # exports REMOVED (KazooTestCase/KazooTestHarness) → breaking change
│   ├── harness.py              # DELETED (legacy hand-rolled harness)
│   ├── common.py               # DELETED (ManagedZooKeeper/ZookeeperCluster + in-process certgen)
│   └── kazoo_ensemble.py       # KEPT + EXTENDED: fixtures, ZkEnsemble.get_client
│                               #   auth→client mapping (R-06), log-dump hook (R-07)
└── tests/
    ├── conftest.py             # pytest hooks (log dump on failure)
    ├── util.py                 # KEPT (CI/wait helpers)
    ├── integ/
    │   ├── conftest.py         # re-exports kazoo_ensemble fixtures + compose file-list selection
    │   ├── docker-compose.base.yml                    # default (plain) 3-node ensemble
    │   ├── docker-compose.auth-digest.yml             # superDigest (via ZK_AUTH_JVMFLAGS)
    │   ├── docker-compose.auth-sasl-digest.yml        # JAAS DigestLoginModule + SASL provider
    │   ├── docker-compose.auth-sasl-gssapi.yml        # KDC + JAAS Krb5 + TLS tunnel + GSSAPI
    │   ├── docker-compose.auth-tls.yml                # certgen + Netty + secureClientPort
    │   ├── dockerfiles/kdc/                           # Alpine KDC (ported from tmp/kdc)
    │   │   ├── Dockerfile
    │   │   └── root/entrypoint.sh
    │   └── test_*.py             # 6 already migrated + new auth/feature tests
    ├── test_election.py          # MIGRATED from KazooTestCase → fixtures
    ├── test_gevent_handler.py    # MIGRATED
    ├── test_interrupt.py         # MIGRATED
    ├── test_lease.py             # MIGRATED
    ├── test_lock.py              # MIGRATED
    ├── test_partitioner.py       # MIGRATED
    ├── test_party.py             # MIGRATED
    ├── test_queue.py             # MIGRATED
    ├── test_sasl.py              # MIGRATED (maps to sasl_digest/sasl_gssapi flavors)
    └── unit/                     # unchanged

setup.cfg                   # [options.extras_require] test: +testcontainers>=4,<5,
                            #   −pytest-docker, −pyjks, −pyOpenSSL;
                            #   − Python 3.8 classifier (test env requires 3.9+)
ensure-zookeeper-env.sh     # DELETED (no longer needed)
init_krb5.sh                # DELETED (replaced by KDC container)
docker-compose.yml / docker-compose-test.yml  # DELETED (superseded by kazoo/tests/integ/...)
zookeeper/                  # removed from CI cache usage
.github/workflows/testing.yml  # REWRITTEN: tiered matrix (FR-017)
CHANGES.md                  # + BREAKING CHANGES entry (legacy kazoo.testing API removed)
```

**Structure Decision**: Single-project layout — the feature lives inside the
existing kazoo package (`kazoo/testing/`, `kazoo/tests/`) plus repo-root CI and
packaging files. No new top-level packages. The compose/KDC artifacts are
collocated with the integration tests (`kazoo/tests/integ/`), keeping the harness
and its fixtures together with the tests they serve.

## Complexity Tracking

> No Constitution Check violations to justify — this section is intentionally empty.
