.. _testing:

=======
Testing
=======

Kazoo's own integration test suite uses a Docker-Compose based test harness
that is also exposed as public API for use in your own tests. The harness
starts a real ZooKeeper ensemble in containers (`zookeeper` image, ≥ 3.7),
waits for it to become healthy, and provides pytest fixtures that give you a
connected :class:`~kazoo.client.KazooClient`.

Requirements
============

* A `docker compose` compatible CLI (v2.12+) with a running Docker daemon.
* Python 3.9+ (Python 3.8 support was dropped).
* No ZooKeeper binary, Java, keytool, or local ZK classpath is required —
  everything runs in containers.

Install the project with the test extras:

.. code-block:: bash

    pip install -e '.[test]'

The harness is implemented in :mod:`kazoo.testing.kazoo_ensemble`. The
`legacy <https://docs.python.org/3/library/unittest.html>`_ ``KazooTestHarness`` /
``KazooTestCase`` API was removed; use the pytest fixtures below instead
(see `CHANGES.md <https://github.com/python-zk/kazoo/blob/main/CHANGES.md>`_
under BREAKING CHANGES).

Entry point
===========

The :class:`~kazoo.testing.kazoo_ensemble.ZkEnsemble` class and the fixtures
it provides are registered as a pytest plugin. To use the harness in your own
``pytest`` suite, import the fixtures from the ensemble module in your
``conftest.py`` (fixtures are plain functions that receive the ensemble):

.. code-block:: python

    from kazoo.testing import kazoo_ensemble

Fixtures
========

``zkensemble``
    Session-scoped. Starts a three-node ZooKeeper ensemble via
    ``docker compose up --wait`` and tears it down (``down --volumes``) at
    session end. Yields a :class:`~kazoo.testing.kazoo_ensemble.ZkEnsemble`
    that can create and manage clients, stop/start individual ensemble
    members (for failure-injection tests), and expose the resolved axis
    configuration.

``zkclient``
    Function-scoped. A started :class:`~kazoo.client.KazooClient` connected
    to the ensemble. The connection options implied by the active
    configuration (auth, features) are applied automatically.

``zkchroot``
    Function-scoped. The chroot path (``/``) under which the test may create
    nodes; it is created and removed around each test to keep runs isolated.

``zksuperadmin_client``
    Function-scoped. A client authenticated with the superDigest digest
    credentials, for tests that need to bypass ACLs.

Example:

.. code-block:: python

    def test_create_and_read(zkclient):
        zkclient.ensure_path("/my/test/path")
        assert zkclient.exists("/my/test/path") is not None

Testing axes
============

The harness exposes three axes as pytest command-line options (each also
honors an environment variable):

``--zk-version`` (or ``ZK_VERSION``)
    ZooKeeper server tag, e.g. ``3.7.2``, ``3.8.3``, ``3.9.5`` (default).

``--zk-auth`` (or ``ZK_AUTH``)
    Authentication flavor: ``plain`` (default), ``digest``, ``sasl_digest``,
    ``sasl_gssapi``, ``tls``. The auth flavor selects the matching
    docker-compose overlay file and the client-side connection options (TLS
    certs, SASL options).

``--zk-features`` (or ``ZK_FEATURES``)
    Comma-separated ZooKeeper feature set: ``standard`` (default), ``ttl``,
    ``readonly``, ``reconfig`` (injected as server JVM flags).

``--zk-features=capture``
    Adds the **capture** harness feature: per-member ``tshark`` sidecars (in an
    in-repo Alpine image, FR-009) record all client-port traffic (clear 2181,
    secure 2281) for the whole session into per-member pcapng artifacts
    (``kazoo-client-zooN-*.pcapng``) under ``${ZK_WORK_DIR}/captures``, which
    survive cluster teardown for later analysis. It composes with any auth
    flavor; on ``tls`` it also emits ``captures/tls/zk-secrets.log`` (an
    SSLKEYLOGFILE) plus the server/CA certificates so the capture decrypts with
    only emitted material. Capture is observational and never changes test
    outcomes. See ``specs/002-network-capture/`` for the full contract and
    quickstart guide.

Compose layout
==============

The compose files live in ``kazoo/tests/integ/``:

* ``docker-compose.base.yml`` — the base three-node ensemble (ephemeral
  ports, tmpfs data dirs, healthcheck).
* ``docker-compose.auth-<auth>.yml`` — per-auth overlay files layered on top
  of the base file (digest, sasl-digest, tls, sasl-gssapi).
* ``dockerfiles/`` — support images for TLS cert generation and the Kerberos
  KDC used by the GSSAPI axis.

The active overlay set is resolved by ``docker_compose_config()`` in
``kazoo/tests/integ/conftest.py`` and the ensemble is driven through
`testcontainers <https://testcontainers-python.readthedocs.io>`_
(:class:`testcontainers.compose.DockerCompose`).

Marker shortcuts
================

The harness registers pytest markers that let you gate tests on the active
axes (they skip with an actionable reason when the active configuration does
not match):

* ``@pytest.mark.zk_version("<3.8")`` — PEP 440 specifier vs. the active ZK
  version.
* ``@pytest.mark.zk_auth("digest", "tls")`` — run only under the listed auth
  schemes.
* ``@pytest.mark.zk_features(require=[...], skip=[...])`` — run only when the
  listed features are (or are not) active.

Zake
====

For those that do not need (or desire) to setup a Zookeeper cluster to test
integration with kazoo there is also a library called
`zake <https://pypi.python.org/pypi/zake/>`_. Contributions to
`Zake's github repository <https://github.com/yahoo/Zake>`_ are welcome.

Zake can be used to provide a *mock client* to layers of your application that
interact with kazoo (using the same client interface) during testing to allow
for introspection of what was stored, which watchers are active (and more)
after your test of your application code has finished.