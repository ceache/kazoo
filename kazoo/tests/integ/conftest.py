from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from kazoo.testing.kazoo_ensemble import (
    FEATURE_JVM_PROPERTIES,
    KazooZkEnv,
    check_skip_version_marker,
    docker_env,
    pytest_addoption as kazoo_ensemble_pytest_addoption,
    pytest_configure as kazoo_ensemble_pytest_configure,
    zkchroot,
    zkclient,
    zkensemble,
    zksuperadmin_client,
)

if TYPE_CHECKING:
    from typing import Any


# These hooks are implemented in kazoo.testing.kazoo_ensemble; local stubs
# re-expose them through this conftest module for pytest's plugin discovery.
def pytest_addoption(parser):
    kazoo_ensemble_pytest_addoption(parser)


def pytest_configure(config):
    kazoo_ensemble_pytest_configure(config)


@pytest.fixture(scope="session")
def docker_compose_config(
    docker_env: KazooZkEnv,
) -> dict[str, Any]:
    """Resolve the docker-compose file for the active auth axis.

    Test-specific selection: compose files are organized per authentication
    flavor under ``docker-compose/<auth>/docker-compose.yml``. If no such
    flavor directory exists yet, fall back to the default compose file in this
    directory.
    """
    auth = docker_env.auth
    flavor_dir = os.path.join(os.path.dirname(__file__), "docker-compose", auth)
    flavor_compose = os.path.join(flavor_dir, "docker-compose.yml")
    if os.path.exists(flavor_compose):
        compose_path = flavor_compose
    else:
        compose_path = os.path.join(os.path.dirname(__file__), "docker-compose.yml")

    # Expose resolved axis values to docker-compose interpolation.
    os.environ["ZK_VERSION"] = docker_env.version
    os.environ["ZK_AUTH"] = auth
    os.environ["ZK_FEATURES"] = ",".join(docker_env.features)
    jvm_flags = []
    for feature in docker_env.features:
        for prop in FEATURE_JVM_PROPERTIES.get(feature, ()):
            jvm_flags.append(prop)
    os.environ["ZK_FEATURES_JVMFLAGS"] = " ".join(jvm_flags)

    return {
        "version": docker_env.version,
        "auth": auth,
        "features": docker_env.features,
        "compose_path": compose_path,
    }


@pytest.fixture(scope="session")
def docker_compose_file(
    docker_compose_config: dict[str, Any],
) -> str:
    return docker_compose_config["compose_path"]
