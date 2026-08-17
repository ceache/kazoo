from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from kazoo.testing.kazoo_ensemble import (
    FEATURE_JVM_PROPERTIES,
    ZKAuthMode,
    ZKFeature,
    KazooZkEnv,
    check_skip_version_marker,
    docker_compose,
    docker_env,
    pytest_addoption as kazoo_ensemble_pytest_addoption,
    pytest_collection_modifyitems as kazoo_ensemble_pytest_collection_modifyitems,  # noqa: E501
    pytest_configure as kazoo_ensemble_pytest_configure,
    pytest_sessionfinish as kazoo_ensemble_pytest_sessionfinish,
    zkchroot,
    zkclient,
    zkensemble,
    zksuperadmin_client,
)

# pytest discovers fixtures by name in conftest modules; re-export the
# ensemble fixtures so the integ tests can request them directly. Declaring
# them in ``__all__`` marks them as intentional re-exports (F401/F811).
__all__ = [
    "check_skip_version_marker",
    "docker_compose",
    "docker_env",
    "zkchroot",
    "zkclient",
    "zkensemble",
    "zksuperadmin_client",
]

if TYPE_CHECKING:
    from typing import Any


# These hooks are implemented in kazoo.testing.kazoo_ensemble; local stubs
# re-expose them through this conftest module for pytest's plugin discovery.
def pytest_addoption(parser):
    kazoo_ensemble_pytest_addoption(parser)


def pytest_configure(config):
    kazoo_ensemble_pytest_configure(config)


def pytest_collection_modifyitems(session, config, items):
    kazoo_ensemble_pytest_collection_modifyitems(session, config, items)


def pytest_sessionfinish(session, exitstatus):
    kazoo_ensemble_pytest_sessionfinish(session, exitstatus)


@pytest.fixture(scope="session")
def docker_compose_config(
    docker_env: KazooZkEnv,
) -> dict[str, Any]:
    """Resolve the docker-compose overlay files for the active axis.

    The base file (``docker-compose.base.yml``) is always included. For any
    non-plain authentication flavor an overlay file
    (``docker-compose.auth-<auth>.yml``) is layered on top via docker-compose
    multi-file support. Interpolation variables (ZK_VERSION,
    ZK_FEATURES_JVMFLAGS, ZK_AUTH_JVMFLAGS, ZK_WORK_DIR,
    COMPOSE_PROJECT_NAME) are exported to the process environment by
    :func:`~kazoo.testing.kazoo_ensemble.docker_env` and
    :func:`~kazoo.testing.kazoo_ensemble._resolve_axis_options` before this
    fixture runs.
    """
    auth = docker_env.auth

    compose_files = ["docker-compose.base.yml"]
    if auth is not ZKAuthMode.PLAIN:
        # The overlay files use a hyphenated flavor name
        # (docker-compose.auth-sasl-digest.yml) while the auth axis value uses
        # an underscore (sasl_digest); map between the two.
        overlay = auth.value.replace("_", "-")
        compose_files.append(f"docker-compose.auth-{overlay}.yml")

    # The capture overlay layers the tshark sidecar (and, on tls, the keylog
    # agent provisioning) on top of whatever base/auth combination is active
    # (R-04).
    if ZKFeature.CAPTURE in docker_env.features:
        compose_files.append("docker-compose.features-capture.yml")

    # Expose resolved axis values to docker-compose interpolation.
    os.environ["ZK_VERSION"] = docker_env.version
    os.environ["ZK_AUTH"] = auth.value
    os.environ["ZK_FEATURES"] = ",".join(f.value for f in docker_env.features)
    jvm_flags = []
    for feature in docker_env.features:
        for prop in FEATURE_JVM_PROPERTIES.get(feature, ()):
            jvm_flags.append(prop)
    os.environ["ZK_FEATURES_JVMFLAGS"] = " ".join(jvm_flags)

    return {
        "version": docker_env.version,
        "auth": auth,
        "features": docker_env.features,
        "compose_files": compose_files,
    }
