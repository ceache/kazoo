# Quickstart: Validating Client Tests & Network Capture

**Feature**: `004-fix-test-client-flakiness` | **Date**: 2026-08-29 | **Spec**: [spec.md](spec.md)

## Overview
This guide provides step-by-step instructions to run, reproduce, and validate the `test_client.py` integration tests, including running the request queuing tests in a loop and using network packet capture for wire protocol diagnostics.

---

## Prerequisites

1. **Docker Engine**: Running locally and accessible via `docker compose`.
2. **Python Environment**: Active virtualenv with requirements installed:
   ```bash
   source venv/bin/activate
   ```

---

## Validation Scenarios

### Scenario 1: Execute Request Queuing Tests Repeatedly

Run both `test_request_queuing_session_expired` and `test_request_queuing_session_recovered` in a loop to verify determinism and absence of flakiness:

```bash
# Run 10 consecutive iterations of the request queuing tests
pytest kazoo/tests/integ/test_client.py -k test_request_queuing -v --count=10
```

**Expected Outcome**:
- `test_request_queuing_session_expired` is executed (NOT skipped) and passes 10/10 times.
- `test_request_queuing_session_recovered` passes 10/10 times.
- Zero assertion errors (`assert 1 == 0` or unhandled exceptions).

---

### Scenario 2: Validate Wire Protocol with Network Capture

Run the request queuing test with packet capture enabled to inspect ZooKeeper wire negotiation:

```bash
pytest kazoo/tests/integ/test_client.py -k test_request_queuing_session_expired --zk-features=capture -s
```

**Expected Outcome**:
- Test passes cleanly.
- Test logs output the path to the capture directory containing `kazoo-capture-zoo1.pcap`.
- Inspecting `kazoo-capture-zoo1.pcap` with `tshark` confirms:
  1. `ConnectRequest` with mangled password.
  2. `ConnectResponse` with `timeOut: 0` (server rejects session).
  3. Subsequent `ConnectRequest` with `sessionId: 0`.
  4. Subsequent `ConnectResponse` with `timeOut: 30000` (new session established).

---

### Scenario 3: Full `test_client.py` Suite Execution

Run the complete integration suite in `test_client.py`:

```bash
pytest kazoo/tests/integ/test_client.py -v
```

**Expected Outcome**:
- 100% of applicable tests pass (only version-incompatible tests for legacy versions skipped via version markers).
- Zero flakes or hangs.

---

### Scenario 4: Static Quality Checks

Verify code styling and static typing:

```bash
flake8 kazoo/tests/integ/test_client.py
black --check kazoo/tests/integ/test_client.py
mypy kazoo/tests/integ/test_client.py
```

**Expected Outcome**:
- Clean output with 0 errors.
