from __future__ import annotations

from kazoo.testing.fixtures import (
    check_skip_version_marker,
    docker_compose,
    docker_compose_config,
    docker_env,
    pytest_addoption as kazoo_fixtures_pytest_addoption,
    pytest_collection_modifyitems as kazoo_fixtures_pytest_collection_modifyitems,  # noqa: E501
    pytest_configure as kazoo_fixtures_pytest_configure,
    pytest_sessionfinish as kazoo_fixtures_pytest_sessionfinish,
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
    "docker_compose_config",
    "docker_env",
    "zkchroot",
    "zkclient",
    "zkensemble",
    "zksuperadmin_client",
]


# These hooks are implemented in kazoo.testing.fixtures; local stubs
# re-expose them through this conftest module for pytest's plugin discovery.
def pytest_addoption(parser):
    kazoo_fixtures_pytest_addoption(parser)


def pytest_configure(config):
    kazoo_fixtures_pytest_configure(config)


def pytest_collection_modifyitems(session, config, items):
    kazoo_fixtures_pytest_collection_modifyitems(session, config, items)


def pytest_sessionfinish(session, exitstatus):
    kazoo_fixtures_pytest_sessionfinish(session, exitstatus)
