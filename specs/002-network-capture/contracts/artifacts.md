# Contract: Capture Artifact Layout

**Feature**: [Network Capture](../spec.md) · **Research**: [research.md](../research.md)

What a capture-enabled session places on disk under the session's temporary
test directory (`ZK_WORK_DIR`, the pytest basetemp) — and what survives after
the run.

## Directory layout

```
${ZK_WORK_DIR}/
├── captures/
│   ├── kazoo-client-zoo1-<ts>.pcapng   # one per member, per run (FR-002)
│   ├── kazoo-client-zoo2-<ts>.pcapng
│   ├── kazoo-client-zoo3-<ts>.pcapng
│   └── tls/                            # present ONLY on the tls auth flavor (US2)
│       ├── zk-secrets.log       # SSLKEYLOGFILE — merged session keys (R-02)
│       ├── server-cert.pem      # server certificate
│       └── ca.pem               # signing CA certificate
└── agent/                              # (US2)
    └── extract-tls-secrets.jar  # pinned keylog agent jar (provisioned, R-10)
```

- Each `kazoo-client-zooN-<ts>.pcapng` is **pcapng** (tshark native, FR-004)
  and contains full-length frames (`-s 0`, FR-005) of the traffic on that
  member's *client ports* (clear `2181`, secure `2281`). The files are unique
  per member AND per invocation (`<ts>` = nanosecond timestamp), so recreated
  sidecars never clobber earlier files (R-08). Together they cover all three
  ensemble members (FR-002) and can be merged into one combined capture with
  `mergecap`.
- `zk-secrets.log` is assembled at teardown from the per-node keylogs the
  agent wrote into the `/logs` bind mounts (`logs/zk1|zk2|zk3/tls-secrets.log`)
  — see [decryption.md](./decryption.md). *(US2)*
- The path is printed to the test output at teardown so developers know where
  to look.

## Retention contract

- The `captures/` directory is a **bind mount** from the host temp dir.
  `docker compose down --volumes` removes only named compose volumes, so the
  artifacts **survive cluster teardown unchanged** (FR-003, FR-009).
- Each pytest session gets a fresh basetemp (+ per-session `COMPOSE_PROJECT_NAME`),
  so successive runs never overwrite one another (FR-012); pytest retains the
  most recent runs' basetemp dirs, making the artifacts available "after the
  tests" for analysis (FR-003).

## Analysis contract

**Plain / digest / sasl_digest / sasl_gssapi axes**: tshark reads each
`kazoo-client-zooN-*.pcapng` directly; the ZooKeeper client protocol is carried
unencrypted on port 2181. (Wireshark ships no built-in ZK dissector — analysis
is at the TCP/traffic level unless a third-party Lua dissector is loaded.)

**tls axis**: decrypt first using the emitted keylog material (see
[decryption.md](./decryption.md)), then analyze the plaintext on port 2281.
*(US2 — pending)*

## Integrity

- A valid artifact is a complete, readable pcapng (verified with
  `capinfos kazoo-client-zooN-<ts>.pcapng`); an interrupted session still
  leaves flushed, readable partial files (edge case, R-05).
- A member with no client connections may produce a small but valid capture —
  never a corrupt or zero-byte file.
- On the tls axis, `zk-secrets.log` must be non-empty after any TLS handshake;
  a missing/empty keylog on a capture-enabled tls run is a failure to report
  (it means the agent never recorded a session). *(US2)*