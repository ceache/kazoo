# Contract: TLS Decryption Material

**Feature**: [Network Capture](../spec.md) · **Research**: [research.md](../research.md)

> **Status: US2 — NOT YET IMPLEMENTED.** This contract describes the planned
> decryption mechanism (tasks T012–T017). The `${ZK_CAPTURE_JVMFLAGS}`
> `-javaagent:` flag computation already exists in `_resolve_axis_options`
> (capture on the tls flavor), but the `tls-secrets-agent` jar provisioning,
> the overlay `depends_on`/mount wiring, and the teardown keylog assembly do
> not — a `tls`+`capture` run today would try to load the not-yet-provisioned
> agent jar. Treat the rest of this document as the design contract for US2,
> not shipped behavior. (The per-member capture artifacts, R-01/R-05/R-08, are
> implemented; the TLS *decryption* material is not.)

How the TLS portions of the capture are decrypted using nothing but what the
harness emits.

## The problem with "just give me the keys"

Modern TLS negotiates forward secrecy (ECDHE) by default: the server's RSA
private key alone cannot decrypt the session, because the symmetric keys are
derived per-session and never leave the two endpoints. Full decryption requires
a **keylog file** (SSLKEYLOGFILE) that records each session's master secret.
Kazoo's client builds its own `ssl.SSLContext` (it never calls
`ssl.create_default_context()`), so the standard `SSLKEYLOGFILE` env-var hook
does not apply to kazoo connections — but the **server side** is Java, and that
is where the secrets can be captured.

## What this feature does instead (R-02)

Under the `capture` axis on the `tls` auth flavor, the three ZooKeeper server
JVMs launch with the **`extract-tls-secrets`** JSSE agent
(`name.neykov:extract-tls-secrets:5.0.0`, Apache-2.0, provisioned in-repo per
R-10):

- The agent is attached at JVM startup via
  `-javaagent:/agent/extract-tls-secrets.jar=<per-node log>` injected through
  the `${ZK_CAPTURE_JVMFLAGS}` interpolation into the base file's
  `SERVER_JVMFLAGS`.
- It hooks the JSSE handshake and writes an SSLKEYLOGFILE-format secrets file
  (`CLIENT_RANDOM <client_random> <master_secret>` lines, plus TLS 1.3 traffic
  secrets when negotiated). Keylog entries are **symmetric**: secrets captured
  on the server side decrypt the session in **both directions**, so every kazoo
  client connection in the capture is covered by the three server logs.
- The TLS channel itself is **not modified**: default ciphers and versions
  (ECDHE, TLS 1.3) stay in force — decryption does not require downgrading the
  suite (FR-007, R-02).

The emitted material lives in `${ZK_WORK_DIR}/captures/tls/`:

| File | Derivation | Use |
|---|---|---|
| `zk-secrets.log` | merged from the per-node keylogs (`logs/zk1|zk2|zk3/tls-secrets.log`) written by the agent | SSLKEYLOGFILE — decrypts the TLS frames |
| `server-cert.pem` | copied from `${ZK_WORK_DIR}/certs/server/` | identifies the server certificate in the trace |
| `ca.pem` | copied from `${ZK_WORK_DIR}/certs/cacert.pem` | trust anchor context |

No private key is exported (FR-011): the keylog *is* the key material.

## Decryption procedure

1. Open the per-member capture files `kazoo-client-zooN-*.pcapng` in Wireshark
   (or merge them first with `mergecap` for a combined view), then decrypt.
2. Wireshark: **Edit → Preferences → Protocols → TLS → "(Pre)-Master-Secret
   log filename"** → select `zk-secrets.log`. tshark equivalent:
   `-o tls.keylog_file:/path/zk-secrets.log`.
3. Reload the capture. TLS frames on port 2281 now decrypt; analyze the
   plaintext ZooKeeper client protocol (handshake magic `\xff\xff\xff\xff`
   followed by the protocol version/magic can be spot-checked in the payload).
4. (Optional) To make the trace portable, inject the keys into the file:
   `editcap --inject-secrets tls,zk-secrets.log cap.pcapng decrypted.pcapng`.

## Hard limits

- Decryption works only for **sessions started while `capture` was active**
  (only those runs attach the agent and record secrets).
- Reusing the keylog on captures from *other* sessions is meaningless — secrets
  are per-session and derived from throwaway PKI (FR-011).
- The agent requires the **JDK JSSE provider**: the harness must never set
  `ssl.sslProvider=OPENSSL`/Conscrypt. The official image defaults to `JDK`,
  which this feature relies on (R-02).
- Java 21+ prints an informational agent-loading notice at startup; it is
  harmless (JEP 451 restricts *dynamic* attach, not startup `-javaagent:`).

## Provider constraint

ZooKeeper selects its TLS provider via `ssl.sslProvider` (since 3.9.0); older
versions always use the JDK provider. The keylog agent hooks exactly the JDK
JSSE internals. A future `sslProvider=OPENSSL` (netty-tcnative/Conscrypt) would
silently defeat decryption — this contract therefore requires the default JDK
provider on any `capture`-enabled tls run.
