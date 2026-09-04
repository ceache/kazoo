# Feature Specification: Network Capture

**Feature Branch**: `002-network-capture`

**Created**: 2026-08-16

**Status**: Draft

**Input**: User description: "let's expand the docker-compose harness with a new feature: network capture. I want to be able to run pytest --zk-feature capture that will result in a network capture artifact in the temp test directory (it must remain after the tests for analysis). The capture should include all traffic to the zoo{1,2,3} client ports (tls and clear), and in the case of tls the ssl certs keys should be made available so that traffic can be decrypted. the capture should be done with tshark on alpine."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Turn on network capture for a test session and keep the artifact (Priority: P1)

A Kazoo developer (or maintainer) investigating a client-server interaction wants a packet trace of a test session. They run the integration suite with the capture capability enabled (e.g. `pytest --zk-features=capture`). The harness provisions its normal 3-node ensemble, records all client-port traffic for the entire session — including traffic to the TLS port when present — and leaves per-member capture artifacts behind in the session's temp directory after the suite ends, so they can be opened later in any standard packet-analysis tool.

**Why this priority**: This is the essence of the feature. Without a persistent, inspectable artifact of all client-port traffic, network capture provides no value. Everything else (TLS decryption, axis compatibility) builds on this.

**Independent Test**: Run a representative slice of the integration suite (e.g. the client auth subset) with capture enabled and with capture disabled. The capture-enabled run must produce a readable capture artifact for each of the three ensemble members, present in the temp directory after the suite exits, while the capture-disabled run behaves exactly as before.

**Acceptance Scenarios**:

1. **Given** a test session started with the capture capability enabled, **When** the suite runs to completion, **Then** capture artifacts covering the client ports of each ensemble member (zoo1, zoo2, zoo3) are produced and **Then** those artifacts remain in the session's temp test directory after the suite exits.
2. **Given** a capture-enabled session, **When** an analyst opens the artifacts in a standard packet-analysis tool (merging them for a combined view), **Then** together they contain traffic to and from all three ensemble members' client ports, spanning the test session, so sequences can be followed across instances (e.g. failover, leader change).
3. **Given** a capture-enabled session with an early/failing test, **When** the suite ends, **Then** the traffic captured so far is still flushed and available for analysis.

---

### User Story 2 - Decrypt captured TLS traffic with the harness-emitted keys (Priority: P1)

A developer debugging a TLS or mTLS interaction (the `tls` auth flavor) wants to inspect the decrypted handshake and session payloads, not just ciphertext. When capture is enabled on a TLS run, the harness also emits the certificate and key material needed to decrypt the captured TLS traffic, stored alongside the artifact. Using only what the harness produced, the analyst can decrypt the secure-port traffic in the combined capture.

**Why this priority**: The whole point of capturing TLS is to analyze what was actually exchanged. Emitting only ciphertext would leave the primary analysis on the TLS axis impossible, so this must ship with the core capability.

**Independent Test**: Run the TLS auth subset with capture enabled. Using only the decryption material emitted by the harness, decrypt the captured TLS client-port traffic (across all three ensemble members) and confirm the plaintext protocol messages are visible.

**Acceptance Scenarios**:

1. **Given** a capture-enabled session on the TLS auth flavor, **When** the suite ends, **Then** the harness has emitted the certificate/key material required to decrypt the captured TLS client-port traffic alongside the capture artifacts.
2. **Given** a capture-enabled TLS session and the emitted decryption material, **When** an analyst performs decryption using a standard packet-analysis tool, **Then** the plaintext protocol traffic is fully visible (no external secrets required).
3. **Given** a capture-enabled session that is *not* using TLS, **When** the suite ends, **Then** no extraneous decryption material is emitted (nothing to decrypt).

---

### User Story 3 - Capture composes cleanly with the full test matrix and lifecycle (Priority: P2)

A CI maintainer enables capture across the version/auth/feature axes and needs to be sure it neither disturbs test results nor collides across runs. The capture capability works together with every existing axis value (plain, digest, sasl_digest, sasl_gssapi, tls; ttl, readonly, reconfig, standard; all supported ZooKeeper versions), starts and stops in step with the cluster lifecycle, and produces per-session capture output that never clobbers previous runs.

**Why this priority**: The harness's value is its matrix coverage. A feature that interferes with test outcomes, breaks on a specific axis, or silently fails to capture would undermine trust in both the harness and the capture output.

**Independent Test**: Run the same test file with capture enabled and disabled across the version/auth/feature matrix; outcomes must be identical, a per-member capture artifact must be produced on every capture-enabled run, and repeated runs must not overwrite each other.

**Acceptance Scenarios**:

1. **Given** capture enabled on any supported version/auth/feature combination, **When** the suite runs, **Then** capture succeeds and test pass/skip/fail outcomes are identical to the same run without capture.
2. **Given** two successive capture-enabled runs, **When** each ends, **Then** each run's artifact lives in its own session directory and neither overwrites the other.
3. **Given** a capture-enabled run where the capture tooling cannot start, **When** the suite begins, **Then** the run fails fast with an actionable message rather than completing with an empty or missing artifact.
4. **Given** a capture-enabled run, **When** the cluster is torn down at session end, **Then** teardown does not delete the capture artifact.

---

### Edge Cases

- Capture on the plain (no-auth), digest, sasl_digest, and sasl_gssapi flavors: only the clear-text client port carries test traffic; the per-member artifacts must still cover that port for all three ensemble members.
- Capture on the TLS flavor: both the clear client port (used by the base healthcheck) and the TLS client port (used by tests) must appear in the per-member artifacts, and decryption material must be emitted.
- Capture combined with the ttl / readonly / reconfig feature flags: capture is orthogonal to server JVM flags and must not affect them (or vice versa).
- Suite aborted mid-run (keyboard interrupt, timeout, cluster failure): traffic captured up to that point remains available.
- Capture tooling footprint versus host platform: the artifact lifecycle must not depend on anything beyond the already-required docker-compose-capable CLI; platform-specific capture behavior is out of scope.
- Long/slow suites generating large capture output: size must remain manageable for the intended analysis (single sessions), with size management decided during planning.
- Large ZooKeeper packets (multi-KB options/TLV payloads): frames MUST be recorded full-length, never truncated to a snaplen, so no packet payload is lost for analysis.
- **Empty artifact** (member with no client connections in the session): must still be a valid, readable capture, not a corrupt or zero-byte partial file.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The testing harness MUST let a user enable network capture for a test session through the existing feature-selection interface (e.g. `--zk-features=capture`), independent of the server feature flags (ttl/readonly/reconfig).
- **FR-002**: When capture is enabled, the harness MUST produce capture artifacts covering the network traffic involving the client ports of all three ensemble members (zoo1, zoo2, zoo3) for the duration of the test session, so that sequences can be followed across instances (e.g. failover, leader change); the artifacts MUST cover the clear-text client port and, when enabled, the TLS client port. One artifact is produced per member (`kazoo-client-zooN-*.pcapng`); the per-member files can be merged into a single combined capture (e.g. with `mergecap`) for a one-file view.
- **FR-003**: The capture artifact MUST be written to the session's temporary test directory and MUST remain there after the test session ends (not removed by the harness or by cluster teardown), ready for later analysis.
- **FR-004**: The capture artifact MUST be in a standard, widely readable capture format that common packet-analysis tools open without conversion.
- **FR-005**: Captured frames MUST be recorded in full (no payload truncation by a maximum capture length), so that ZooKeeper's options/TLV packet structure is preserved for analysis.
- **FR-006**: When the run uses the TLS auth flavor, the harness MUST emit — in the same session output area as the artifact — the certificate and key material required to decrypt the captured TLS client-port traffic.
- **FR-007**: Enabling capture MUST NOT change any test outcome, skip decision, or client-visible connection behavior relative to an otherwise identical run without capture.
- **FR-008**: Capture MUST start before the first test and stop/flush in step with the cluster lifecycle; if capture cannot start, the harness MUST fail the run with an actionable explanation rather than completing with a missing or empty artifact.
- **FR-009**: Cluster teardown and session cleanup MUST NOT delete the capture artifact or decryption material.
- **FR-010**: The capture tooling MUST be provisioned from an in-repo, versioned definition (matching how the harness provisions its other infrastructure such as the KDC), so the capture environment is reproducible and auditable; the capture tool MUST be `tshark` running on Alpine Linux, per the user's explicit constraint.
- **FR-011**: Any decryption material emitted by the harness MUST come from the harness's existing throwaway test PKI (certificates, or session secrets derived from handshakes over that PKI) — no real credentials, keytabs, or production keys — and MUST NOT be logged or printed into test output.
- **FR-012**: Repeated capture-enabled runs MUST produce per-session, uniquely identified capture output that does not overwrite previous runs.

### Key Entities

- **Capture artifact**: The per-member packet trace file for a test session
  (`kazoo-client-zooN-*.pcapng`), in a standard readable capture format,
  covering the client ports of one ensemble member, produced by the capture
  tooling and preserved after the run. Together the per-member artifacts cover
  all three ensemble members and can be merged into a single combined capture.
- **Decryption material**: The certificate/key files emitted alongside the artifact for TLS sessions, sufficient to decrypt the captured TLS traffic.
- **Session output directory**: The temporary directory for a given test session where the artifact and decryption material are placed and preserved.
- **Ensemble member**: One of the three ZooKeeper servers (zoo1/zoo2/zoo3) whose client-port traffic is included in the per-member capture artifacts.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of capture-enabled runs produce a readable capture artifact for **each** ensemble member, and 100% of those artifacts are still present immediately after the suite exits.
- **SC-002**: In 100% of capture-enabled runs, an analyst can open the artifacts with a standard packet-analysis tool and observe traffic to/from all three client ports spanning the session (merging the per-member files for a combined view), so sequences can be followed across instances (e.g. failover, leader change).
- **SC-003**: In 100% of capture-enabled runs, captured ZooKeeper packet payloads (including multi-KB options/TLV frames) are complete — no frame is truncated — so full-packet analysis is possible.
- **SC-004**: For 100% of capture-enabled TLS runs, traffic on the TLS client port can be decrypted to plaintext using only the material the harness emitted.
- **SC-005**: Test pass/skip/fail outcomes are identical between capture-enabled and capture-disabled runs across the version/auth/feature matrix.
- **SC-006**: In 100% of capture-enabled runs, capture either succeeds or the run aborts at startup with an actionable message — never a silent empty/missing artifact.
- **SC-007**: Running the suite with capture enabled on the same axis produces per-member artifacts, with no cross-run overwrites or leftover debris between runs.

## Assumptions

- The capture capability is exposed through the existing `--zk-features` feature-selection flag (e.g. `--zk-features=capture`), as the user specified, even though capture is a harness capability rather than a ZooKeeper server feature; the mechanics of wiring it into the axis are a planning decision.
- The capture tool is `tshark` running on Alpine Linux, built from an in-repo Dockerfile (consistent with the in-repo KDC image pattern), per the user's explicit technology constraint.
- Artifact granularity is a **per-member capture collection** (one
  `kazoo-client-zooN-<ts>.pcapng` per ensemble member, uniquely named per
  member and per run), per the planning decision that this is the
  lifecycle-correct form: each member's network namespace is owned by a
  netns-holder container that is never stopped by failure-injection tests, so
  per-member capture survives member restarts deterministically. The per-member
  files can be merged (e.g. with `mergecap`) into a single combined capture to
  follow sequences across instances (failover/leader change).
- Captured frames are recorded in full (no snaplen truncation), per the user's requirement to accommodate ZooKeeper's options/TLV packet structure; artifact size therefore grows with real payload volume and size management for very long suites (rotation or capping) is addressed during planning, not in this specification.
- On the `tls` auth flavor the existing harness PKI already provides server keystores/truststores and client cert/key files; "decryption material" builds on that throwaway PKI. Where ephemeral key exchange would defeat private-key decryption, planning determines the exact mechanism (e.g. session-key export) that still satisfies FR-006/SC-004. **Planning decision**: session-key **export via a JSSE keylog agent attached to the server JVMs** (`extract-tls-secrets`, Apache-2.0) — the TLS channel stays at default ciphers, decryption uses only the emitted SSLKEYLOGFILE plus the throwaway certs (R-02).
- The feature does not relax any harness guarantee: still a fixed 3-node ensemble, still requires only a docker-compose-capable CLI, and capture is fully compatible with the version/auth/feature axes.