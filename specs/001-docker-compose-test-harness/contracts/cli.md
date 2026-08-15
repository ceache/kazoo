# Contract: Pytest CLI & Environment Variables

**Feature**: [Docker-Compose Test Harness](../spec.md)

The harness exposes the three testing axes through pytest CLI options (with
environment-variable fallbacks). All options are optional; defaults produce a
plain-auth, standard-feature run on the latest pinned ZooKeeper version.

## CLI options

| Option | Values | Default | Env fallback | Description |
|---|---|---|---|---|
| `--zk-version` | `3.7.x`, `3.8.x`, `3.9.x` tags | `3.9.4` | `ZK_VERSION` | Official image tag for the ensemble (FR-003) |
| `--zk-auth` | `plain`, `digest`, `sasl_digest`, `sasl_gssapi`, `tls` | `plain` | `ZK_AUTH` | Auth scheme; selects the compose flavor (FR-004) |
| `--zk-features` | comma-separated subset of `standard`, `ttl`, `readonly`, `reconfig` | `standard` | `ZK_FEATURES` | Server feature toggles (FR-005) |

## Environment variables set by the harness

These are exported before the compose process runs and are consumed by compose
interpolation:

| Variable | Value | Purpose |
|---|---|---|
| `ZK_VERSION` | resolved image tag | `${ZK_VERSION}` in compose files |
| `ZK_AUTH` | resolved auth flavor | selects the compose overlay (`docker-compose.auth-<auth>.yml`) |
| `ZK_FEATURES` | comma-joined features | logging/reporting |
| `ZK_FEATURES_JVMFLAGS` | space-joined JVM flags for the active features | `${ZK_FEATURES_JVMFLAGS}` in `SERVER_JVMFLAGS` |
| `ZK_AUTH_JVMFLAGS` | auth JVM flags (e.g. superDigest for `digest`), empty otherwise | `${ZK_AUTH_JVMFLAGS}` in `SERVER_JVMFLAGS` |
| `ZK_WORK_DIR` | pytest session temp dir (`tmp_path_factory`) | host bind-mount target for container logs, certs, and keytabs |
| `COMPOSE_PROJECT_NAME` | `kazoo-<uuid8>`, unique per pytest session | isolates the compose project across concurrent sessions |

## Environment variables consumed from the host (by the harness / tests)

| Variable | Purpose |
|---|---|
| `KRB5_CONFIG` | path to the shared `krb5.conf` produced by the KDC (sasl_gssapi) |
| `KRB5_CLIENT_KTNAME` | path to the Kazoo client keytab produced by the KDC (sasl_gssapi) |

## Examples

```bash
# Baseline (plain auth, standard features, ZK 3.9.4)
pytest kazoo/tests/integ

# ZK 3.8 with SASL digest auth
pytest kazoo/tests/integ --zk-version=3.8.3 --zk-auth=sasl_digest

# ZK 3.9 with TTL + reconfig features
pytest kazoo/tests/integ --zk-version=3.9.4 --zk-features=ttl,reconfig

# GSSAPI inside a TLS-validated tunnel
pytest kazoo/tests/integ --zk-auth=sasl_gssapi
```

## Validation

- `--zk-auth` is constrained to the five enumerated values; an unknown value is
  a pytest option error.
- Feature flags not in the supported set are ignored for JVM-flag injection but
  reported.
- When neither the CLI option nor the env var is set, the documented default is
  used (FR-003/004/005).

See also [fixtures.md](./fixtures.md) and [compose.md](./compose.md).
