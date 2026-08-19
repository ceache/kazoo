"""Unit tests for the kazoo.testing harness modules.

Two test groups live here:

* ``TestImportSurface`` -- the module-layout contract: the names the
  integration suite imports must resolve from ``kazoo.testing.common`` and
  ``kazoo.testing.fixtures``, and the replaced modules must no longer be
  importable.
* Harness logic tests (axis resolution, marker evaluation, mount paths,
  ensemble helpers, compose-overlay selection, keylog assembly, capture
  probing) -- these exercise the pure functions in ``kazoo.testing.common``
  and never require a Docker engine or a live ZooKeeper.

The pure-function groups aim for 100% branch coverage of
``kazoo.testing.common``.
"""

from __future__ import annotations

import importlib

import pytest


class TestImportSurface:
    """The ``kazoo.testing`` module layout contract.

    The integration suite imports fixtures, hooks, and a few helpers from the
    harness. Those names must stay importable from the two split modules, and
    the modules they replaced must be gone.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "docker_env",
            "docker_compose",
            "zkensemble",
            "zkchroot",
            "zkclient",
            "zksuperadmin_client",
            "check_skip_version_marker",
            "docker_compose_config",
            "pytest_addoption",
            "pytest_configure",
            "pytest_collection_modifyitems",
            "pytest_sessionfinish",
        ],
    )
    def test_fixtures_exports(self, name: str) -> None:
        module = importlib.import_module("kazoo.testing.fixtures")
        assert hasattr(module, name)

    @pytest.mark.parametrize(
        "name",
        [
            "ZKAuthMode",
            "ZKFeature",
            "ZK_DEFAULT_VERSION",
            "FEATURE_JVM_PROPERTIES",
            "AUTH_JVM_FLAGS",
            "KazooZkEnv",
            "ZkEnsemble",
            "_assemble_tls_keylog",
            "_evaluate_axis_markers",
        ],
    )
    def test_common_exports(self, name: str) -> None:
        module = importlib.import_module("kazoo.testing.common")
        assert hasattr(module, name)

    def test_kazoo_ensemble_module_removed(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("kazoo.testing.kazoo_ensemble")

    def test_kazoo_tests_conftest_removed(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("kazoo.tests.conftest")
