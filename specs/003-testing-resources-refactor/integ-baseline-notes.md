# T002 / T030-V6 integration baseline notes (LOCAL, uncommitted)

Captured on macOS (darwin), Python 3.10.19, docker-compose ZK ensemble.

## T002 baseline (post-refactor, per T030-V6 reruns)

| Axis                          | Command                                          | Passed | Skipped | Failed/Errored |
|-------------------------------|--------------------------------------------------|--------|---------|----------------|
| plain / standard features     | `pytest kazoo/tests/integ -q --zk-auth=plain`    | 375    | 33      | 0              |
| tls auth                      | `pytest kazoo/tests/integ -q --zk-auth=tls`      | 377    | 31      | 0              |
| capture feature               | `pytest kazoo/tests/integ -q --zk-auth=plain --zk-features=capture` | 379 | 29 | 0              |
| full suite, digest axis       | `ZK_AUTH=digest pytest kazoo/tests -q` (unit+integ, as CI test_axes) | 568 | 32 | 0              |

## CI test_axes regression (unit test env-sensitivity)

CI's `test_axes` legs run `pytest kazoo/tests/` with `ZK_AUTH=<axis>` in the process env. The
session-scoped `docker_env` from the integration side exports the resolved axes into os.environ,
so `TestDockerEnvFixture::test_fixed_per_member_ports` (which asserts pure defaults) resolved the
leaked `ZK_AUTH=digest` / `sasl_gssapi` instead. Fixed by dropping the axis vars inside that test
before invoking the fixture; reproduced locally with `ZK_AUTH=digest`. The digest combined run
above passes; sasl_gssapi cannot be replicated on this macOS host (needs host kinit + libkrb5).

## CI pytest-version regression in TestFixtureHooks.test_addoption_registers_axes

`Parser._groups[0]` is private; under the tox-pinned pytest 8.4.2 the group list starts empty
(IndexError), while the latest pytest (9.1.1) uses different error types for invalid choices. The
test now asserts registration behaviorally via `Parser.parse_known_args` (accepts the axis values,
rejects unknown auth values, defaults to None) and passes on both pytest 8.4.2 and 9.1.1. Full unit
dir: 193 passed / 1 skipped on both.

Notes:
- tls counts (377/31) are AFTER the `KazooClient.command()` fix below.
- The tls axis runs 2 more selected tests than plain (test_sasl sasl_plain/digest under TLS); the
  capture axis runs the capture-probe tests that the standard axis skips.
- No pre-refactor baseline could be captured (spec work was already merged when this notes file was
  started); T030-V6 green-run counts above are the parity reference.
- The `docker_collect` / batched-output-format parity check: the symbol does not exist anywhere in the
  repo (rg over the tree and full git history returns nothing); all harness output is streaming
  (print mode), so there is no batched/print divergence to verify.

## Pre-existing upstream bug found while running the tls axis

`KazooClient.command()` in `kazoo/client.py` computed:

```python
peer = self._connection._socket.getpeername()[:2]
peer_host = self._connection._socket.getpeername()[1]   # BUG: port, not host
```

`getpeername()[1]` is the remote *port* (an int). Passing it as `hostname=` into
`create_connection(..., use_ssl=True)` sends an integer server_name for TLS SNI, so
`ssl.SSLSocket._create()` raised `ValueError` / `TypeError` whenever a TLS client issued
`server_version()` (e.g. `test_queue._skip_unless_zk34`, and `test_client`'s
`test_server_version`/`test_command`). This is out of scope for the 003 refactor (it predates it)
but blocks the tls axis of the target integ suite, so it was fixed:

- `kazoo/client.py`: `peer_host = peer[0]` (reuses `peer`; passes the host address).
- regression unit test: `kazoo/tests/unit/test_client_command.py`.
- `pyproject.toml`: added `'kazoo.tests.unit.test_client_command'` to the mypy override list.
- CHANGES.md Unreleased/Bug Fixes/core note added.
