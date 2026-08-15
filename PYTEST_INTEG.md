## Architectural Overview

This solution drives **Docker Compose programmatically within Pytest** (via `testcontainers-python` or `pytest-docker`) while managing test execution across three distinct dimensions.

To maximize test execution speed and reliability:

* **Container Lifecycle (Session-Scoped):** Pytest launches and health-checks a single Docker Compose environment tailored to the active session parameters once per test run.
* **Test Isolation (Ephemeral Chroot):** Tests share the running ZooKeeper instance but execute within a unique ephemeral chroot (e.g., `/test_<uuid>`), which is cleaned up in milliseconds during test teardown.
* **Test Filtering (Collection-Time Skipping):** Tests declare their environmental constraints via custom pytest markers. Pytest inspects the active cluster configuration during collection and automatically skips tests that do not match the environment.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           GitHub Actions / CLI                          │
│     Matrix: Python Version × ZK Version × Auth Scheme × Feature Set     │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ (Passes CLI Flags / Env Vars)
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Pytest Session                                                          │
│  ├─▶ [Session Fixture] Boots Docker Compose via Testcontainers          │
│  │   • Injects: ZK Version, Auth (JAAS/Certs), Feature flags            │
│  │   • Waits for TCP / 4-letter-word healthcheck                        │
│  │                                                                      │
│  ├─▶ [Collection Hook] Evaluates @pytest.mark.zk against active config  │
│  │   • Mismatched tests marked Skipped with reasons                     │
│  │                                                                      │
│  ├─▶ [Function Fixture] zk_client (Per-Test)                            │
│  │   • Connects KazooClient(hosts="localhost:<port>/test_<uuid>")       │
│  │   • Yields client ──▶ Runs Test                                      │
│  │   • Teardown: Deletes chroot tree in ms                              │
│  │                                                                      │
│  └─▶ [Session Teardown] Tears down Docker Compose stack                 │
└─────────────────────────────────────────────────────────────────────────┘

```

---

## 1. The Three Testing Axes

| Axis | Dimensions / Options | Description |
| --- | --- | --- |
| **1. ZooKeeper Version** | `3.7`, `3.8`, `3.9` | Parameterizes the container image tag (`zookeeper:${ZK_VERSION}`). |
| **2. Authentication** | `plain`, `digest`, `sasl_digest`, `sasl_gssapi`, `tls` | Controls port security, PKI cert generation, and KDC/JAAS sidecars. |
| **3. Features** | `standard`, `ttl`, `readonly`, `reconfig` | Controls ZooKeeper JVM/system flags (e.g., `zookeeper.extendedTypesEnabled=true`). |

---

## 2. Pytest Harness Implementation (`conftest.py`)

This configuration adds CLI options to pytest, controls the session-scoped container lifecycle, skips tests based on marker requirements, and provides an isolated client fixture per test.

```python
# conftest.py
import uuid
import pytest
from packaging.version import parse as parse_version
from kazoo.client import KazooClient
from testcontainers.compose import DockerCompose

# 1. Custom CLI Flags
def pytest_addoption(parser):
    parser.addoption("--zk-version", default="3.9", help="ZooKeeper version (e.g., 3.7, 3.8, 3.9)")
    parser.addoption("--zk-auth", default="plain", choices=["plain", "digest", "sasl_digest", "sasl_gssapi", "tls"])
    parser.addoption("--zk-features", default="standard", help="Comma-separated features (e.g., ttl,readonly,reconfig)")

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "zk(min_version, max_version, auth, require_features, skip_features): Configure ZooKeeper test prerequisites"
    )

# 2. Collection-Time Dynamic Skipping Hook
def pytest_collection_modifyitems(config, items):
    env_version = parse_version(config.getoption("--zk-version"))
    env_auth = config.getoption("--zk-auth")
    env_features = set(config.getoption("--zk-features").split(","))

    for item in items:
        zk_mark = item.get_closest_marker("zk")
        if not zk_mark:
            continue

        kwargs = zk_mark.kwargs
        
        # Check minimum / maximum version requirements
        if "min_version" in kwargs and env_version < parse_version(kwargs["min_version"]):
            item.add_marker(pytest.mark.skip(reason=f"Requires ZK >= {kwargs['min_version']}, running {env_version}"))
        if "max_version" in kwargs and env_version > parse_version(kwargs["max_version"]):
            item.add_marker(pytest.mark.skip(reason=f"Requires ZK <= {kwargs['max_version']}, running {env_version}"))

        # Check authentication requirements
        if "auth" in kwargs:
            required_auth = [kwargs["auth"]] if isinstance(kwargs["auth"], str) else kwargs["auth"]
            if env_auth not in required_auth:
                item.add_marker(pytest.mark.skip(reason=f"Requires auth in {required_auth}, running {env_auth}"))

        # Check required features (Must be present)
        if "require_features" in kwargs:
            req = set(kwargs["require_features"] if isinstance(kwargs["require_features"], list) else [kwargs["require_features"]])
            missing = req - env_features
            if missing:
                item.add_marker(pytest.mark.skip(reason=f"Missing required feature(s): {missing}"))

        # Check incompatible features (Skip if present)
        if "skip_features" in kwargs:
            incompatible = set(kwargs["skip_features"] if isinstance(kwargs["skip_features"], list) else [kwargs["skip_features"]])
            present = incompatible & env_features
            if present:
                item.add_marker(pytest.mark.skip(reason=f"Test incompatible with active feature(s): {present}"))

# 3. Session-Scoped Docker Compose Fixture
@pytest.fixture(scope="session")
def zk_environment(request):
    zk_version = request.config.getoption("--zk-version")
    zk_auth = request.config.getoption("--zk-auth")
    zk_features = request.config.getoption("--zk-features")

    # Map auth profile to the appropriate compose file or profile
    compose_file = "docker-compose.yml"
    env_vars = {
        "ZK_VERSION": zk_version,
        "ZK_AUTH": zk_auth,
        "ZK_FEATURES": zk_features,
    }

    with DockerCompose(".", compose_file_name=compose_file, env_file=None) as compose:
        # Wait for the client port to accept traffic
        host = compose.get_service_host("zookeeper", 2181)
        port = compose.get_service_port("zookeeper", 2181)
        
        yield {
            "hosts": f"{host}:{port}",
            "auth": zk_auth,
            "version": zk_version,
            "features": zk_features.split(","),
        }

# 4. Function-Scoped Isolated Kazoo Client Fixture
@pytest.fixture
def zk_client(zk_environment):
    test_chroot = f"/test_{uuid.uuid4().hex[:12]}"
    base_hosts = zk_environment["hosts"]
    
    # 1. Connect root client to create the chroot znode
    root_client = KazooClient(hosts=base_hosts)
    root_client.start()
    root_client.ensure_path(test_chroot)
    root_client.stop()
    root_client.close()

    # 2. Yield client scoped strictly to the ephemeral chroot
    scoped_client = KazooClient(hosts=f"{base_hosts}{test_chroot}")
    scoped_client.start()

    yield scoped_client

    # 3. Teardown: wipe the chroot tree
    scoped_client.stop()
    scoped_client.close()

    cleanup_client = KazooClient(hosts=base_hosts)
    cleanup_client.start()
    if cleanup_client.exists(test_chroot):
        cleanup_client.delete(test_chroot, recursive=True)
    cleanup_client.stop()
    cleanup_client.close()

```

---

## 3. Test Annotations & Examples

Use the `@pytest.mark.zk(...)` marker to declare environmental requirements. Tests without markers run across all baseline configurations.

```python
import pytest
from kazoo.protocol.states import KazooState

# Case 1: Generic test - Runs on any standard cluster
def test_basic_crud_operations(zk_client):
    zk_client.create("/node_a", b"data")
    assert zk_client.get("/node_a")[0] == b"data"
    zk_client.delete("/node_a")


# Case 2: Version-Gated Test (Requires ZK >= 3.8)
@pytest.mark.zk(min_version="3.8")
def test_modern_zk_api(zk_client):
    # Validates functionality introduced in ZK 3.8+
    pass


# Case 3: Auth-Specific Test (Requires GSSAPI/Kerberos)
@pytest.mark.zk(auth="sasl_gssapi")
def test_kerberos_authentication_handshake(zk_client):
    assert zk_client.state == KazooState.CONNECTED
    # Verify SASL authorization context


# Case 4: Feature-Gated Test (Run ONLY if 'ttl' feature is present)
@pytest.mark.zk(require_features=["ttl"], min_version="3.8")
def test_create_ttl_node(zk_client):
    # ZooKeeper extended types / TTL nodes
    zk_client.create("/ttl_node", b"payload", makepath=True, ttl=5000)
    assert zk_client.exists("/ttl_node") is not None


# Case 5: Feature-Incompatibility Test (SKIP if 'readonly' feature is active)
@pytest.mark.zk(skip_features=["readonly"])
def test_write_heavy_transaction(zk_client):
    for i in range(10):
        zk_client.create(f"/bulk_write_{i}", b"data")

```

---

## 4. Pytest CLI Examples

```bash
# Run baseline tests against ZooKeeper 3.9 (Plain Auth, Standard Features)
pytest --zk-version=3.9 --zk-auth=plain --zk-features=standard

# Run against ZooKeeper 3.7 with SASL-Digest authentication
pytest --zk-version=3.7 --zk-auth=sasl_digest --zk-features=standard

# Run against ZooKeeper 3.8 with TTL and Reconfig features enabled
pytest --zk-version=3.8 --zk-auth=plain --zk-features=ttl,reconfig

# Run TLS mutual auth test suite against ZooKeeper 3.9
pytest --zk-version=3.9 --zk-auth=tls --zk-features=standard

```

---

## 5. GitHub Actions Matrix Configuration

This workflow tests supported Python versions against targeted ZooKeeper configurations. It uses a **tiered strategy** to avoid running every redundant permutation while thoroughly validating all auth schemes and features on the latest runtime targets.

```yaml
name: CI Matrix (Python x ZooKeeper)

on:
  push:
    branches: [main, master]
  pull_request:
    branches: [main, master]

jobs:
  test:
    name: "Py ${{ matrix.python-version }} | ZK ${{ matrix.zk-version }} | Auth: ${{ matrix.zk-auth }} | Feat: ${{ matrix.zk-features }}"
    runs-on: ubuntu-latest

    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.9", "3.10", "3.11", "3.12", "3.13"]
        zk-version: ["3.7", "3.8", "3.9"]
        zk-auth: ["plain"]
        zk-features: ["standard"]

        # Tier 1 & 2: Include specialized Auth and Feature configurations
        include:
          # Auth permutations on Python 3.12 + ZK 3.9
          - python-version: "3.12"
            zk-version: "3.9"
            zk-auth: "sasl_digest"
            zk-features: "standard"
          - python-version: "3.12"
            zk-version: "3.9"
            zk-auth: "sasl_gssapi"
            zk-features: "standard"
          - python-version: "3.12"
            zk-version: "3.9"
            zk-auth: "tls"
            zk-features: "standard"

          # Feature permutations (e.g. TTL nodes on ZK 3.8 and 3.9)
          - python-version: "3.12"
            zk-version: "3.8"
            zk-auth: "plain"
            zk-features: "ttl,reconfig"
          - python-version: "3.12"
            zk-version: "3.9"
            zk-auth: "plain"
            zk-features: "ttl,reconfig"

    steps:
      - name: Checkout Code
        uses: actions/checkout@v4

      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: "pip"

      - name: Install Dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -e .[test]
          pip install testcontainers pytest-docker cryptography

      - name: Execute Pytest Suite
        run: |
          pytest -v \
            --zk-version=${{ matrix.zk-version }} \
            --zk-auth=${{ matrix.zk-auth }} \
            --zk-features=${{ matrix.zk-features }}

```

Using `packaging.specifiers.SpecifierSet` is the standard way to handle version constraints in Python tooling. It gives you full PEP 440 expression power (e.g., `>=3.8, <4.0`, `~=3.8.0`, `!=3.7.2`) without having to manually parse and compare version tuples.

You can extend this exact fixture pattern into a **unified constraint-evaluator fixture** that checks all 3 axes (ZooKeeper version, authentication mode, and active feature flags) before any test logic or client connection runs.

---

### 1. Unified Environment Evaluator Fixture

By making this an `autouse=True` fixture (or having your `zk_client` fixture depend on it), it runs during the setup phase of each test and skips immediately if any constraint fails.

```python
import pytest
from packaging import specifiers, version
from dataclasses import dataclass
from typing import Set

@dataclass
class KazooZkEnv:
    version: str          # e.g. "3.8.2"
    auth: str             # e.g. "sasl_gssapi", "plain", "tls"
    features: Set[str]    # e.g. {"ttl", "reconfig"}

@pytest.fixture(autouse=True)
def check_zk_constraints(request: pytest.FixtureRequest, docker_env: KazooZkEnv) -> None:
    """
    Evaluates test markers against the active Docker ensemble:
      - @pytest.mark.zk_version(">=3.8") or ("<3.8")
      - @pytest.mark.zk_auth("sasl_gssapi", "sasl_digest")
      - @pytest.mark.zk_features(require=["ttl"], skip=["readonly"])
    """
    # ---------------------------------------------------------
    # 1. Version Check (SpecifierSet: supports '>=3.8', '<3.5', etc.)
    # ---------------------------------------------------------
    version_marker = request.node.get_closest_marker("zk_version")
    if version_marker:
        condition_string = version_marker.args[0]
        specifier = specifiers.SpecifierSet(condition_string)
        current_ver = version.Version(docker_env.version)

        if current_ver not in specifier:
            pytest.skip(
                f"Requires ZK version matching '{specifier}', active ensemble is {current_ver}"
            )

    # ---------------------------------------------------------
    # 2. Authentication Check (Allowed vs Disallowed)
    # ---------------------------------------------------------
    auth_marker = request.node.get_closest_marker("zk_auth")
    if auth_marker:
        # e.g., @pytest.mark.zk_auth("sasl_gssapi", "sasl_digest") or skip="plain"
        allowed_auth = auth_marker.args
        disallowed_auth = auth_marker.kwargs.get("skip", [])
        if isinstance(disallowed_auth, str):
            disallowed_auth = [disallowed_auth]

        if allowed_auth and docker_env.auth not in allowed_auth:
            pytest.skip(
                f"Requires auth in {allowed_auth}, active ensemble uses '{docker_env.auth}'"
            )

        if docker_env.auth in disallowed_auth:
            pytest.skip(
                f"Incompatible with auth '{docker_env.auth}'"
            )

    # ---------------------------------------------------------
    # 3. Features Check (Required vs Incompatible)
    # ---------------------------------------------------------
    feature_marker = request.node.get_closest_marker("zk_features")
    if feature_marker:
        # e.g., @pytest.mark.zk_features(require=["ttl"], skip=["readonly"])
        required = set(feature_marker.kwargs.get("require", []))
        incompatible = set(feature_marker.kwargs.get("skip", []))

        missing = required - docker_env.features
        if missing:
            pytest.skip(f"Missing required feature(s): {missing}")

        present_incompatible = incompatible & docker_env.features
        if present_incompatible:
            pytest.skip(f"Incompatible with active feature(s): {present_incompatible}")

```

---

### 2. Register Markers in `pytest.ini` or `pyproject.toml`

To prevent pytest from raising warnings about unregistered custom markers:

```toml
# pyproject.toml
[tool.pytest.ini_options]
markers = [
    "zk_version(spec): SpecifierSet version constraint (e.g. '>=3.8', '<3.6')",
    "zk_auth(*allowed, skip=None): Required or excluded authentication schemes",
    "zk_features(require=None, skip=None): Required or excluded ZooKeeper feature flags",
]

```

---

### 3. Usage Examples in Tests

```python
import pytest

# Case 1: Complex Version Constraints using PEP 440 / SpecifierSet
@pytest.mark.zk_version(">=3.8, <3.10")
def test_new_38_api(zk_client):
    """Runs only on ZooKeeper 3.8.x and 3.9.x."""
    pass


# Case 2: Multi-Auth Compatibility
@pytest.mark.zk_auth("sasl_digest", "sasl_gssapi")
def test_sasl_authenticated_session(zk_client):
    """Runs only when SASL is active, skips on plain or TLS."""
    pass


# Case 3: Incompatible Auth Exclusion
@pytest.mark.zk_auth(skip="tls")
def test_unencrypted_packet_inspection(zk_client):
    """Runs on all auth setups except mutual TLS."""
    pass


# Case 4: Requiring and Excluding Specific Features
@pytest.mark.zk_version(">=3.8")
@pytest.mark.zk_features(require=["ttl"], skip=["readonly"])
def test_create_ttl_znode(zk_client):
    """Runs only if ZK >= 3.8, TTL is enabled, and cluster is not in Read-Only mode."""
    zk_client.create("/ttl_node", b"data", makepath=True, ttl=3000)
    assert zk_client.exists("/ttl_node") is not None

```

---

### Why the Fixture-Based `pytest.skip()` Works Well Here

1. **Evaluates Runtime State:** If your `docker_env` fixture discovers or validates parameters directly from the running ensemble (e.g., via 4-letter words `srvr` / `stat` or dynamic port inspects), the fixture-based approach evaluates *actual runtime state* rather than just static CLI strings.
2. **Short-Circuits Setup:** By marking `check_zk_constraints` with `autouse=True`, pytest evaluates the marker rules before test-level fixtures (like opening expensive client socket connections) execute.
