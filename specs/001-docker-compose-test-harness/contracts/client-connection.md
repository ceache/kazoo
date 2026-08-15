# Contract: Client Connection Mapping

**Feature**: [Docker-Compose Test Harness](../spec.md)

`ZkEnsemble.get_client(**kwargs)` returns a `KazooClient` whose connection
options are implied by the active auth axis (FR-004), unless the caller
explicitly overrides them. This is the harness-side contract that keeps test
code agnostic to the security configuration.

## Mapping

| Auth axis | Implied KazooClient options |
|---|---|
| `plain` | none |
| `digest` | `auth_data=[("digest", "super:super_secret")]` |
| `sasl_digest` | `sasl_options={"mechanism": "DIGEST-MD5", "username": "jaasuser", "password": "jaas_password"}` (the JAAS `DigestLoginModule` only accepts the hardcoded test users) |
| `sasl_gssapi` | `use_ssl=True` + `certfile`/`keyfile`/`ca` from `${ZK_WORK_DIR}/certs` **and** `sasl_options={"mechanism": "GSSAPI"}` with `KRB5_CONFIG`, `KRB5_CLIENT_KTNAME`, and `KRB5CCNAME=FILE:<fresh per-run ccache>` set (GSSAPI inside the TLS-validated tunnel, FR-012) |
| `tls` | `use_ssl=True` + `certfile`/`keyfile`/`ca` from `${ZK_WORK_DIR}/certs` |

## Precedence

- Explicit `kwargs` (e.g. `auth_data=`, `sasl_options=`, `use_ssl=`, `hosts=`)
  win over the implied defaults.
- `hosts` defaults to `get_hosts()` (all three nodes, comma-joined).
- `superadmin=True` appends `("digest", "super:super_secret")` to the caller's
  `auth_data` list (for the digest superDigest setup).

## Caller responsibility

- Clients created via `get_client` are **not** auto-started/closed by fixtures
  unless the caller uses `zkclient`/`zksuperadmin_client`; callers must
  `client.start()` / `client.stop()` / `client.close()`.
- Callers using TLS/GSSAPI flavors read credential material from `docker_env.workdir`
  (certs, keytabs, `krb5.conf`); the fixture exports the required env vars.
- For `sasl_gssapi` the fixture (`_export_krb5_client_env`) also exports a
  **fresh per-run FILE credential cache** via `KRB5CCNAME`, populated with
  `kinit -c <file> -kt <client.keytab> client@EXAMPLE.ORG`. This prevents the
  macOS default `API:...` ccache from reusing stale tickets minted by a
  previous KDC instance (each compose stack runs its own realm with new keys),
  which the fresh server cannot decrypt (`Checksum failed`).
- The `zkensemble` fixture normalizes wildcard publisher bind hosts
  (`0.0.0.0`/`::`) to `127.0.0.1`: the GSSAPI service principal is derived
  from the connect host (`zookeeper@<host>`), and the KDC provisions
  `zookeeper/127.0.0.1` + `zookeeper/localhost` only.

## Validation

- `get_client()` connects on every auth flavor and every supported ZK version.
- Wrong/absent credentials are rejected (negative auth tests) — FR-013 keeps all
  credentials throwaway.
- A caller-provided option is never silently overwritten by an implied default.

See also [compose.md](./compose.md) and [fixtures.md](./fixtures.md).
