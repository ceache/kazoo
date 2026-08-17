# Quickstart: Validating Network Capture

**Feature**: [Network Capture](./spec.md) · **Data model**: [data-model.md](./data-model.md)
· **Contracts**: [contracts/](./contracts/) · **Research**: [research.md](./research.md)

This guide proves the feature works end-to-end. It is a validation/run guide —
implementation details live in `tasks.md` and the implementation phase.

---

## Prerequisites

- A docker-compose compatible CLI (`docker compose` v2.12+) with a running
  Docker daemon (same as the parent harness).
- Python 3.9+; project installed with test extras:
  ```bash
  pip install -e '.[test]'
  ```
- No ZooKeeper binary, Java, `tshark`, or Wireshark on the host — the capture
  tool runs in the in-repo Alpine container (FR-009).

## Validation scenarios (map to Success Criteria)

### V1 — Per-member capture artifacts appear and survive the run (SC-001, SC-002, SC-003)

1. Run any slice with capture enabled (plain auth):
   ```bash
   pytest kazoo/tests/integ/test_client.py -k "test_create or test_auth" --zk-features=capture -v
   ```
2. **After** the suite exits, find the artifacts in the pytest basetemp
   (printed at teardown): `${ZK_WORK_DIR}/captures/kazoo-client-zooN-*.pcapng`.
3. Verify:
   ```bash
   ls <path>/captures/                          # kazoo-client-zoo1/2/3-*.pcapng
   capinfos <path>/kazoo-client-zoo1-*.pcapng   # valid, complete pcapng
   tshark -r <path>/kazoo-client-zoo1-*.pcapng -c 5
   ```
   **Expected**: one artifact per member (a member with no client connection
   may be small); frames to/from each member's client ports; the per-member
   files merge into one combined capture with `mergecap`; no leftover harness
   containers (`docker ps` clean).

   > **Note on Docker Desktop**: while the session is *running*, the host-side
   > view of an open, actively-written pcapng can be stale (virtiofs syncs the
   > mount back only once the writing process exits). The authoritative
   > end-of-session `capinfos` gate is this post-run check. Mid-session
   > validation (the self-check tests) inspects the files **inside the capture
   > containers** via `docker compose exec`, which is always current.

### V2 — TLS traffic decrypts with only the emitted material (SC-004, FR-006) *(US2 — pending)*

1. Run the TLS axis with capture:
   ```bash
   pytest kazoo/tests/integ/test_client.py -k "test_create or test_auth" --zk-auth=tls --zk-features=capture -v
   ```
2. After the run, the TLS material must exist:
   ```bash
   ls <basetemp>/captures/tls/     # zk-secrets.log, server-cert.pem, ca.pem
   ```
3. Decrypt and spot-check the plaintext handshake magic:
   ```bash
   tshark -o tls.keylog_file:<basetemp>/captures/tls/zk-secrets.log \
     -r <basetemp>/captures/kazoo-client-zooN-*.pcapng -Y "tls" \
     -T fields -e tcp.payload | grep -c "ffffffff"
   ```
   **Expected**: the connect-request magic appears in decrypted payloads; no
   external secrets used (single keylog file, no private key exported).

> **Note**: V2 depends on the JSSE keylog agent (US2, tasks T012–T017), which
> is not yet implemented. Until then the TLS-axis capture produces ciphertext
> artifacts without emitted decryption material.

### V3 — Non-TLS runs emit no decryption material (FR-006 edge)

Repeat V1 with `--zk-auth=digest`. **Expected**: `captures/tls/` absent; the
per-member artifacts still cover the 2181 client port.

### V4 — Capture does not change test outcomes (SC-005, FR-007)

```bash
pytest kazoo/tests/integ/test_client.py -k "test_create or test_auth" -q            # baseline
pytest kazoo/tests/integ/test_client.py -k "test_create or test_auth" --zk-features=capture -q
```
**Expected**: identical pass/skip/fail results for the identical selection.

### V5 — Combined features (FR-001, FR-007)

```bash
pytest kazoo/tests/integ/test_client.py --zk-features=ttl,capture -q
pytest kazoo/tests/integ/test_client.py --zk-auth=sasl_digest --zk-features=reconfig,capture -q
```
**Expected**: capture works layered with server features and auth overlays;
outcomes match the same run without capture.

### V6 — Fail fast when capture cannot start (SC-006, FR-008/FR-010)

Temporarily break the capture image (e.g. force a bad `FROM` in
`dockerfiles/capture/Dockerfile`), then run with `--zk-features=capture`.
**Expected**: the session aborts at startup with an actionable message (e.g.
"capture: in-repo image build failed before the stack started ... check Docker
network / registry reachability for dockerfiles/capture (apk tshark)"), no
tests execute, and `docker ps` shows no leftover stack.

### V7 — Full-length frames, no truncation (SC-003, FR-005)

With capture on, create/discover a multi-KB ZooKeeper value in the suite
(full-length options/TLV frames) and confirm frames are not capped:
```bash
capinfos <artifact> | grep -i snaplen     # or compare data length vs captured length
```
**Expected**: snaplen is unlimited (-s 0); multi-KB ZK payloads are captured in
full in the per-member artifacts.

### V8 — Per-session isolation, no clobbering (SC-007, FR-012)

Run the same capture command twice. **Expected**: per-member artifacts under
distinct session basetemp dirs, uniquely named per member AND per run; the
second run does not overwrite the first; no harness debris between runs.

### V9 — Interrupted session still leaves a flushed artifact (edge)

Start a capture run and interrupt it (Ctrl-C) mid-suite. **Expected**: the
pcapng up to the interruption is present, readable, and flushes cleanly
(FR-003).