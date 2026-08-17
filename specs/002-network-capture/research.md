# Research: Network Capture (2026-08-16)

All `NEEDS CLARIFICATION` items from the plan's Technical Context are resolved
here. Decisions are recorded as R-01..R-10 and cited from `plan.md`'s
Constitution Check and the feature's `contracts/`.

---

## R-01 — Capture topology: per-member tshark sidecars in each member's network namespace

> **Design revision (2026-08-16, during implementation)**: an earlier draft of
> this decision used a **single promiscuous** tshark on the compose bridge
> writing one `kazoo-client.pcapng`. That approach was validated first and then
> **rejected** for lifecycle reasons (below). The shipped design is the
> Kubernetes **pause-container pattern**: each member's network namespace is
> owned by a holder container, and both the ZK JVM and a capture sidecar share
> it. Capture is therefore **per member** — a collection of
> `kazoo-client-zooN-<ts>.pcapng` files (R-05/R-08).

**Unknown**: The capture must include traffic to the client ports (clear 2181,
TLS 2281) of **all three** ensemble members, cross platform (Linux, macOS/
Windows Docker Desktop), and must **survive member restarts** (failure-injection
tests stop/start individual ZK processes mid-session).

**Decision**: Split each member into a **netns-holder** (`zooN`) plus a **ZK
JVM service** (`zooN-service`) that joins the holder's network namespace via
`network_mode: service:zooN` (`docker-compose.base.yml`). One **capture
sidecar per member** (`zooN-capture` in `docker-compose.features-capture.yml`)
joins the same netns and taps the member's own `eth0` **non-promiscuously**
(`-p`):

```
zoo1-capture:                     # same for zoo2-capture, zoo3-capture
  build: ./dockerfiles/capture
  network_mode: service:zoo1      # shares zoo1's netns (and thus its eth0)
  cap_add: [NET_RAW, NET_ADMIN]   # live capture on a foreign-interface tap
  command: >
    zoo1 -i eth0 -p -s 0
    -f "tcp port 2181 or tcp port 2281"
  volumes:
    - ${ZK_WORK_DIR}/captures:/captures
```

(the entrypoint consumes the member name as `$1` and appends
`-w /captures/kazoo-client-${name}-$(date +%s%N).pcapng`).

**Rationale**:
- **Member restarts must not kill the tap.** The holder container is the
  netns owner; it publishes the member's client ports and never gets stopped by
  `ZkEnsemble.stop`/`start` (the harness maps `zooN` → `zooN-service` via
  `_process_service`, so failure injection only stops the JVM). The holder —
  and therefore the capture sidecar's tap — stays alive across member
  restarts, and the sidecar continues writing into the same file.
- The capture is attached to the **member's own interface**, not the bridge, so
  `-p` (non-promiscuous) suffices: every client→server frame addressed to that
  member's ports crosses its `eth0`. The frame set for a member is exact, and
  the JVM in the same netns listens on exactly the published ports.
- This works identically on Linux and Docker Desktop: containers always run
  inside the Docker VM's Linux networking stack; there is no host-platform
  code path (FR-011 of the parent harness).
- `-s 0` = **no snaplen truncation** (full-length frames, FR-005), satisfying
  ZooKeeper's options/TLV wire format.
- A member with no client connection (the kazoo client connects to whichever
  member it picks) may produce a small capture; the union of frames across the
  three members covers the client-port traffic — mergeable into a single
  combined file with `mergecap` when a combined view is wanted.

**Alternatives considered (rejected)**:
- **Single promiscuous capture on the compose bridge** (original draft):
  validated, then rejected because a bridge-level tap gives no way to stop a
  member's ZK process without either losing capture continuity or
  over-capturing; the holder pattern is the deterministic, lifecycle-correct
  design.
- **Host-namespace capture** (`network_mode: host`): works on Linux but relies
  on host interfaces and root; rejected because it breaks the multiplatform CLI
  guarantee and the no-host-tools constraint.

**Verification**: each `zooN-capture` container holds open a readable
`kazoo-client-zooN-*.pcapng` whose frames include client-port traffic (ports
2181/2281); `stop zooN-service` leaves the sidecar (and file) untouched.

---

## R-02 — TLS decryption: session-key export via a JSSE keylog agent *(US2 — IMPLEMENTED)*

> **Status**: implemented across tasks T012–T017. `_resolve_axis_options`
> computes the `-javaagent:` flag into `${ZK_CAPTURE_JVMFLAGS}` when `capture`
> is active on the `tls` flavor; the `tls-secrets-agent` sidecar provisions the
> pinned/checksummed jar into the shared `${ZK_WORK_DIR}/agent` mount (zoo
> services `depends_on` its `.ready` healthcheck and mount the jar read-only);
> `_assemble_tls_keylog` merges the three per-node keylogs + context certs into
> `captures/tls/` at teardown (and is exercised mid-session by the T012
> self-check). The text below remains the design contract.

**Unknown**: Modern TLS uses forward secrecy (ECDHE); the server's RSA private
key alone cannot decrypt a session. The user requires "ssl certs keys made
available so traffic can be decrypted," and kazoo's client creates its own
`ssl.SSLContext` (`handlers/utils.py:229`), so the standard `SSLKEYLOGFILE`
env-var hook does not apply on the client side.

**Decision**: Under the `capture` axis **on the tls auth flavor**, decrypt by
**exporting session secrets from the server JVMs** with the `extract-tls-secrets`
Java agent (neykov, Apache-2.0, pinned `5.0.0` on Maven Central):

1. Each zoo JVM launches with
   `-javaagent:/agent/extract-tls-secrets.jar=<secrets log path>`, appended to
   `SERVER_JVMFLAGS` via a `${ZK_CAPTURE_JVMFLAGS}` interpolation in
   `docker-compose.base.yml` (alongside the existing
   `${ZK_FEATURES_JVMFLAGS}` / `${ZK_AUTH_JVMFLAGS}`), set **only** when
   `capture` is active on the `tls` flavor.
2. The agent hooks the JSSE handshake and writes an SSLKEYLOGFILE-format
   secrets file (`CLIENT_RANDOM` + `MASTER_SECRET` lines). Keylog entries are
   **symmetric**: secrets captured on the server side decrypt the session in
   **both directions**, covering every kazoo client connection in the capture.
3. Each node writes its own log into the already-writable per-node
   `${ZK_WORK_DIR}/logs/zk<id>/tls-secrets.log` bind mount (no new permission
   handling); the harness merges the three into
   `${ZK_WORK_DIR}/captures/tls/zk-secrets.log` at teardown.
4. The server channel is **not modified** — default ciphers and versions
   (ECDHE, TLS 1.3) stay in force, so FR-007 holds with **no behavioral
   exception at all** (better than the RSA-pinning alternative, which
   downgraded the negotiated suite).
5. `server-cert.pem` and `ca.pem` are exported from the throwaway PKI for
   context (matching the user's "certs keys made available"); **no private key
   is exported** — the keylog is what decrypts.

**Rationale**:
- Wireshark/tshark decrypt TLS via a keylog file universally: it is
  PFS-safe and supports TLS 1.3 (via the newer `CLIENT_HANDSHAKE_TRAFFIC_SECRET`
  lines), unlike RSA-key decryption (RSA key-exchange cipher only, no
  resumption). The agent produces exactly that format.
- **Provider fit**: `extract-tls-secrets` requires the OpenJDK built-in JSSE
  (Conscrypt is *not* supported). Our stack satisfies this by construction:
  the official `zookeeper` image ships no netty-tcnative, so `ssl.sslProvider`
  defaults to `JDK`, and the harness never sets `ssl.sslProvider=OPENSSL`.
- **Java fit**: the agent supports "all supported Java versions" of OpenJDK
  JSSE; the official ZK 3.7/3.8/3.9 images run OpenJDK-based JVMs. Startup
  attach (`-javaagent:` at launch) works on all of them; Java 21 emits only an
  informational agent-loading notice (JEP 451 warning applies to *dynamic*
  attach, not startup attach).
- The spec's Assumptions explicitly permit "session-key export" where
  ephemeral key exchange defeats private-key decryption; this implements
  exactly that mechanism.
- FR-011 intent (throwaway material only): the keylog holds ephemeral session
  secrets derived from handshakes over the throwaway PKI — no real
  credentials, never logged/printed into test output.

**Alternatives considered**:
- **RSA key-exchange pinning + private-key export** (earlier plan): forces
  `TLSv1.2` + `TLS_RSA_WITH_AES_128_CBC_SHA`, changes the exercised TLS channel
  (a FR-007 exception), breaks on resumption, and requires a `ciphers=` kwarg
  injection into kazoo. Rejected once the keylog route was verified.
- **Client-side keylog** (`SSLContext.keylog_filename`, Python 3.8+):
  kazoo constructs its own `ssl.SSLContext`, so the env-var/attribute path does
  not apply; patching the library is invasive. Rejected — the server-side
  agent avoids touching the client at all.
- **Client-side proxy/mitm**: violates no-tools-on-host and would alter the
  server the tests exercise. Rejected.

**Verification**: after `--zk-auth=tls --zk-features=capture`, decryption works
via the standard keylog preference (Wireshark Preferences → Protocols → TLS →
"(Pre)-Master-Secret log filename", or
`tshark -o tls.keylog_file:<zk-secrets.log> -r <artifact>`). The decrypted
bytes are checked for the ZooKeeper connect handshake magic (`\xff\xff\xff\xff`
connect request followed by the protocol magic) on TCP port 2281; in automation
`tshark -o tls.keylog_file:... -r cap.pcapng -Y "tls" -T fields -e tcp.payload`
is grepped for that magic.

---

## R-03 — Capture tooling: tshark on Alpine, provisioned in-repo

**Unknown**: The user requires "tshark on alpine"; the harness must not rely on
third-party image trust (matches the KDC pattern FR-018).

**Decision**: In-repo Dockerfile `kazoo/tests/integ/dockerfiles/capture/`:

```
FROM alpine:3.20                       # pinned, ~small
RUN apk add --no-cache tshark
COPY capture-entrypoint.sh /capture-entrypoint.sh
ENTRYPOINT ["/capture-entrypoint.sh"]  # consumes the member name as $1
```

- `alpine` officially ships `tshark` in the `community` repo (`apk add tshark`),
  carrying `dumpcap`/`mergecap`/Wireshark dissectors.
- The entrypoint script takes the **member name** as `$1` (the overlay passes it
  as the first word of `command:`), builds the uniquely-named output path
  `/captures/kazoo-client-${name}-$(date +%s%N).pcapng`, and `exec`s tshark with
  `-i eth0 -p -s 0 -f "tcp port 2181 or tcp port 2281"` appending `-w <path>`
  (R-08). `tshark -w` flags must never appear in the compose `command:` itself.
- Note on protocol dissection: Wireshark does **not** ship a built-in ZooKeeper
  dissector (MR !528 has been open/WIP since 2020; only a third-party Lua
  dissector, `zab_dissector`, exists). This feature therefore does *not*
  promise automatic ZK-frame rendering in tshark; the guaranteed analysis
  surface is full-length TCP payloads (FR-005) that protocols such as the
  ZooKeeper connect handshake can be verified against byte-for-byte (e.g. the
  four-byte protocol magic), and TLS decryption exposing those payloads as
  plaintext when the emitted keylog is applied (R-02). Plugin-level dissection
  is explicitly out of scope (noted under Assumptions).
- Alpine keeps the image small (KDC precedent); the tool is pinned in-repo, so
  the capture environment is reproducible and auditable (FR-009 of this
  feature).

**Verification**: the image builds via `docker build kazoo/tests/integ/dockerfiles/capture`
and `tshark -v` prints a Wireshark banner.

---

## R-04 — Feature-axis wiring: `capture` is a harness feature, not a server JVM feature

**Unknown**: `--zk-features` values (standard/ttl/readonly/reconfig) map to ZK
JVM flags via `FEATURE_JVM_PROPERTIES`. `capture` has no JVM flag.

**Decision**:
- Add `ZKFeature.CAPTURE = "capture"` to `kazoo/testing/kazoo_ensemble.py`.
- Keep `FEATURE_JVM_PROPERTIES` keyed only to the four *server* features; the
  JVM-flag loop already emits nothing for members absent from the dict
  (`FEATURE_JVM_PROPERTIES.get(feature, ())`), so `capture` contributes no
  flags and leaves `ZK_FEATURES_JVMFLAGS` unchanged — server behavior is
  bit-identical (FR-007).
- The single exception is the **passive keylog agent** (R-02): on the tls
  flavor with `capture` active, `${ZK_CAPTURE_JVMFLAGS}` carries the
  `-javaagent:` startup flag. This is observational only — it records handshake
  secrets without altering connection behavior, ciphers, or outcomes, so FR-007
  remains intact. It is injected through the same host-computed interpolation
  mechanism as `ZK_FEATURES_JVMFLAGS`/`ZK_AUTH_JVMFLAGS`, never hard-coded in
  an overlay.
- `docker_compose_config` (integ/conftest.py) appends
  `docker-compose.features-capture.yml` when `capture` is active, exactly like
  the auth overlays; the overlay layers the capture sidecar for any
  base/auth combination.
- The `zk_features` marker machinery operates on the features tuple, so a
  self-check test can use `@pytest.mark.zk_features(require=["capture"])`
  without any marker-system change.

**Verification**: `pytest ... --zk-features=capture` builds the overlay list
`[base, auth-<x>?, features-capture]`; `docker compose config` validates.

---

## R-05 — Artifact lifecycle: persist past teardown, flush before removal

**Unknown**: `compose.stop()` runs `docker compose down --volumes`; capture
files must survive it.

**Decision**:
- Capture output goes to a **bind mount** `${ZK_WORK_DIR}/captures` (host
  dir). `down --volumes` removes only *named* volumes, never bind mounts, so
  the artifacts survive teardown by construction (FR-003/FR-009).
- Each sidecar writes a **per-member, per-invocation** file
  (`kazoo-client-zooN-<ts>.pcapng`); a recreated sidecar (e.g. after a member
  restart) starts a fresh timestamped file, never clobbering a previous one.
- `docker compose down` sends SIGTERM to the capture sidecars; tshark's
  signal handler performs a clean final flush of the pcapng file. Compose
  `stop_grace_period` default (10s) is ample. The harness's existing teardown
  path is therefore sufficient; no ordering change needed. (Contingency: if a
  flush storm is ever observed, `docker compose stop zooN-capture` before
  `down` is the documented fallback.)

**Verification**: after `pytest ... --zk-features=capture`, the artifacts exist
under the pytest basetemp and `capinfos <artifact>` reports a complete, valid
file; a second run in another session dir doesn't collide (FR-012).

---

## R-06 — Live capture needs extra capabilities, checked at compose level

**Decision**: each capture sidecar declares `cap_add: [NET_RAW, NET_ADMIN]` in
the overlay (compose handles it). The `-p` (non-promiscuous) flag is used, but
capturing on the interface requires `NET_RAW` (open the tap / BPF) **and**
`NET_ADMIN` (set the interface into capture state) — `NET_RAW` alone fails with
"Couldn't run dumpcap in child process: Operation not permitted". Readiness:
each capture sidecar is a long-running service; compose `up --wait` treats it
as ready when running (no healthcheck needed), consistent with the existing
orchestration contract.

**Verification**: `docker compose up --wait` completes with the capture
sidecars running; `docker ps` shows `zoo1-capture`/`zoo2-capture`/`zoo3-capture`
up for the whole session.

---

## R-07 — Pre-flight: fail fast if capture can't start (FR-010 of this feature)

**Decision**: if the `capture` axis is active, the harness builds the capture
image during the `docker_compose` session fixture, before `up`, via
`docker compose build` in the capture overlay's context
(`_build_capture_images` in `kazoo_ensemble.py`). A build/start failure
raises before any test runs, with an actionable message (e.g. "capture:
in-repo image build failed before the stack started ... check Docker network /
registry reachability for dockerfiles/capture (apk tshark)"). This reuses the
existing fixture's `finally` teardown guarantee (`down --volumes` always runs),
so a partial stack is still cleaned up.

**Verification**: deliberately breaking the capture Dockerfile makes the run
abort at session startup with an actionable message (quickstart V6).

---

## R-08 — Deterministic artifact naming per member and per session

**Decision**: capture file names are **unique per member and per invocation**:
`kazoo-client-zooN-<ts>.pcapng`, where `<ts>` is `date +%s%N` (nanosecond
timestamp) from the capture entrypoint. Per-session uniqueness additionally
comes from `${ZK_WORK_DIR}/captures/`, where `ZK_WORK_DIR` is already unique
per pytest session (tmp basetemp + `COMPOSE_PROJECT_NAME`). A recreated
sidecar therefore never clobbers its own earlier file, and no cross-run
clobbering occurs without extra naming logic (FR-012).

**Verification**: two successive runs produce artifacts under distinct
session dirs with distinct member files; `git status` / filesystem shows no
leftover harness debris (SC-007).

---

## R-09 — Decryption material: ephemeral session keys + context certs *(US2 — IMPLEMENTED)*

**Decision**: decryption material is emitted only under the tls flavor with
`capture` active, in `${ZK_WORK_DIR}/captures/tls/`:

| File | Derivation | Use |
|---|---|---|
| `zk-secrets.log` | merged from the three per-node keylogs written by `extract-tls-secrets` into the `/logs` bind mounts | SSLKEYLOGFILE — the actual decryption key material |
| `server-cert.pem` | copied from `${ZK_WORK_DIR}/certs/server/` | identifies the server certificate in the trace |
| `ca.pem` | copied from `${ZK_WORK_DIR}/certs/cacert.pem` | trust anchor context |

No new credentials are generated; **no private key is exported** (the keylog
replaces it). The keylog holds ephemeral session secrets derived from
handshakes over the existing throwaway PKI — throwaway test material, never
logged or printed (FR-011).

---

## R-10 — Provisioning the keylog agent jar: in-repo, pinned, no third-party image trust *(US2 — IMPLEMENTED)*

**Unknown**: the `-javaagent:` jar must exist inside every zoo container
*before* the JVM starts, yet the harness provisions nothing from third-party
images and downloads nothing onto the host.

**Decision**: a `tls-secrets-agent` sidecar service in the capture overlay,
mirroring the certgen pattern:

```yaml
services:
  tls-secrets-agent:
    build: ./dockerfiles/tls-secrets-agent
    volumes:
      - ${ZK_WORK_DIR}/agent:/agent
    healthcheck:
      test: ["CMD-SHELL", "test -f /agent/.ready"]
      interval: 2s
      timeout: 2s
      retries: 60
```

with an in-repo Dockerfile that downloads the **pinned, checksum-verified** jar
at build time (so the fetch happens once, in the image build, exactly like
`apk add tshark` in the capture image — never at session runtime):

```
FROM alpine:3.20
RUN wget -q -O /agent-src/extract-tls-secrets.jar \
      https://repo1.maven.org/maven2/name/neykov/extract-tls-secrets/5.0.0/extract-tls-secrets-5.0.0.jar \
    && echo "015418eaf3ac0832909296af67fa3ec5149c53a075ead6cb29460b17db331ab0  /agent-src/extract-tls-secrets.jar" \
       | sha256sum -c -
# entrypoint: install jar to /agent and touch /agent/.ready
```

- `zoo{1,2,3}-service` gain `depends_on: tls-secrets-agent: condition: service_healthy`
  and a read-only bind mount of the jar:
  `${ZK_WORK_DIR}/agent/extract-tls-secrets.jar:/agent/extract-tls-secrets.jar:ro`.
- The version (`5.0.0`) and SHA-256 (`015418eaf3…31ab0`) are recorded in the
  Dockerfile, so the agent environment is reproducible and auditable — the same
  supply-chain discipline as the pinned base images and `apk` packages
  (FR-009/FR-010).
- If the download/checksum or the `tls-secrets-agent` build fails, the existing
  fail-fast preflight aborts the session with an actionable message (R-07).

**Verification**: `docker compose config` with the capture overlay validates;
zoo healthchecks pass only after the jar is present; the ZK JVM starts with the
agent and a keylog file appears after the first TLS handshake.