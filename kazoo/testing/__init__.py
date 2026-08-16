"""Testing utilities for running integration tests against ZooKeeper.

The legacy ``KazooTestCase`` / ``KazooTestHarness`` public API was removed
(see CHANGES.md under BREAKING CHANGES). Integration tests now use the
pytest fixtures provided by :mod:`kazoo.testing.kazoo_ensemble`
(``zkclient``, ``zkensemble``, ``zkchroot``, ...) which orchestrate a
Docker-Compose ZooKeeper ensemble.
"""

from kazoo.testing import kazoo_ensemble  # noqa: F401


__all__ = (
    "kazoo_ensemble",
)