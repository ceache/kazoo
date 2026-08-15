# Contract: Compose Files & Official Image Interface

**Feature**: [Docker-Compose Test Harness](../spec.md)

The harness configures the official `zookeeper` image (hub.docker.com/_/zookeeper)
**exclusively** through its public interfaces (FR-002); no `zoo.cfg`, log4j file,
or direct Java launch.

## Official image interface used

| Env var / setting | Purpose | Used by flavors |
|---|---|---|
| `ZOO_MY_ID` | server id (`1`/`2`/`3`) | all |
| `ZOO_SERVERS` | quorum + client endpoints (`server.N=0.0.0.0:2888:3888;2181 ...`) | all |
| `ZOO_4LW_COMMANDS_WHITELIST` | `ruok`,`srvr`,`stat`,`mntr`,`conf`,`isro` | all |
| `ZOO_ADMINSERVER_ENABLED` | disable embedded admin server | all |
| `ZOO_STANDALONE_ENABLED` | standalone mode toggle | all |
| `SERVER_JVMFLAGS` | superDigest, extended types, readonly, reconfig JVM props + `${ZK_FEATURES_JVMFLAGS}` | digest/features |
| `JVMFLAGS` | `-Djava.security.auth.login.config=...`, krb5 config | sasl_digest / sasl_gssapi |
| `ZOO_CFG_EXTRA` | `authProvider.1`, `requireClientAuthScheme=sasl`, `secureClientPort`, Netty factory, `ssl.*`, readonly mode | sasl_* / tls |
| `ZOO_LOG_DIR` / `ZOO_LOG4J_PROP` | log location across 3.7–3.9 | all |

## Healthcheck contract

- **Command**: `CMD-SHELL echo 'ruok' | nc -w 2 127.0.0.1 2181 | grep -q imok`
  (verified present in the official image: Ubuntu base ships `nc` + `bash`).
- **Tuning**: interval ~3s, timeout ~3s, generous retries (≈45–60s budget)
  covering first-boot + image pull.

## Compose layout (base + auth overlays)

```
kazoo/tests/integ/
├── conftest.py
├── docker-compose.base.yml                    # default (plain) 3-node ensemble
├── docker-compose.auth-digest.yml             # superDigest (via ZK_AUTH_JVMFLAGS)
├── docker-compose.auth-sasl-digest.yml        # JAAS DigestLoginModule + SASL provider
├── docker-compose.auth-sasl-gssapi.yml        # KDC sidecar + JAAS Krb5 + TLS transport
├── docker-compose.auth-tls.yml                # certgen sidecar + Netty + secureClientPort
└── dockerfiles/kdc/                           # Alpine KDC (in-repo, from tmp/kdc)
```

The active file list is `[base]` + (`[auth-<mode>]` when auth != plain),
selected and validated by `docker_compose_config` (`docker compose config` with
the same `-f` list).

## Orchestration contract (testcontainers `DockerCompose`)

- Instantiated in the session fixture with `context=kazoo/tests/integ` and
  `compose_file_name=<active file list>`.
- `start()` → `docker compose up --wait` (Compose v2.12+): readiness is
  healthcheck-driven; services without a healthcheck are ready when running.
- `stop()` → `docker compose down --volumes` at session teardown.
- Ports/hosts: `get_service_host(service, port)` / `get_service_port(service,
  port)` resolve the **ephemeral** bound ports (`0:2181`, `0:<secureClientPort>`).
- Per-node control (FR-009): `compose_command_property + ["stop", "zooN"]` /
  `["start", "zooN"]`, run via the harness helper.
- Logs (FR-015): `get_logs("zoo1", "zoo2", "zoo3", ...)` → `(stdout, stderr)`.
- Session isolation: `COMPOSE_PROJECT_NAME=kazoo-<uuid8>` is exported before any
  compose command so concurrent sessions cannot collide.

## Shared invariants (all flavors)

- Ephemeral host ports: `0:2181` (client) and, where applicable, `0:<secureClientPort>`
  (TLS); testcontainers `get_service_port` resolves the bound ports.
- tmpfs data/datalog volumes per node; `${ZK_WORK_DIR}/logs` bind-mounted for
  host-side log access (FR-015).
- `${ZK_WORK_DIR}` bind mounts shared with sidecars for certs (tls) and keytabs
  + `krb5.conf` (sasl_gssapi).
- `SERVER_JVMFLAGS` is defined **only** in the base file as
  `${ZK_FEATURES_JVMFLAGS} ${ZK_AUTH_JVMFLAGS}` (compose merges `environment`
  maps wholesale per key, so overlays must not set it); feature toggles come
  from `ZK_FEATURES_JVMFLAGS`, auth JVM flags (superDigest) from
  `ZK_AUTH_JVMFLAGS`. Overlays set `JVMFLAGS` (JAAS) and `ZOO_CFG_EXTRA`
  (providers/security config).
- Sidecar readiness is ordered with `depends_on: condition: service_healthy`
  (KDC healthcheck = exported keytabs present; certgen healthcheck = keystore
  files present).

## Auth flavor composition (server side)

| Flavor | Compose additions | Client contract |
|---|---|---|
| plain | — | plain TCP 2181 |
| digest | superDigest in `SERVER_JVMFLAGS` | `auth_data digest super:super_secret` |
| sasl_digest | JAAS `DigestLoginModule` + SASL provider | `sasl_options DIGEST-MD5` |
| sasl_gssapi | KDC sidecar + JAAS `Krb5LoginModule` + **TLS transport** | `use_ssl` + `sasl_options GSSAPI` (tunneled) |
| tls | certgen sidecar + Netty + `ssl.*` + `X509AuthenticationProvider` | `use_ssl` with client cert/key/ca |

## Validation

- `docker compose config` parses for every flavor on all three ZK version series
  (via `compose_command_property + ["config"]` with the active `-f` list).
- `docker compose up --wait` completes (all healthchecks pass) before tests run
  (FR-006).
- Keytabs / certs / `krb5.conf` generated inside sidecars are world-readable in
  the shared `${ZK_WORK_DIR}` mount so host clients can consume them.

See also [cli.md](./cli.md) and [client-connection.md](./client-connection.md).
