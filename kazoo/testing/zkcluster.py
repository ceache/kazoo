from __future__ import annotations

import time
import tempfile
import subprocess

from jinja2 import Environment

CLUSTER_CONF = None
CLUSTER_DEFAULTS = {
    "ZOOKEEPER_PORT_OFFSET": 20000,
    "ZOOKEEPER_CLUSTER_SIZE": 3,
    "ZOOKEEPER_OBSERVER_START_ID": -1,
}

def main():
    env = Environment()
    with open ("kazoo/testing/templates/docker-compose.yml.jinja") as fp:
        t = env.from_string(fp.read())

    with tempfile.NamedTemporaryFile(mode="wt", delete=False) as conf:
        print("DEBUG: %r" % conf.name)
        conf.write(
            t.render(
                zookeeper_version="3.8",
                zookeeper_num_parts=3,
                zookeeper_num_members=3,
                zookeeper_num_servers=3,
                use_kdc=True,
                zookeeper_work_dir="/tmp",
            )
        )
        conf.flush()

        try:
            subprocess.check_call(
                [
                    "/usr/local/bin/docker-compose",
                    "-f", conf.name,
                    "up",
                    "--force-recreate",
                    "--renew-anon-volumes",
                    "--remove-orphans",
                    "--wait",
                ]
            )
            time.sleep(5)

        finally:
            subprocess.check_call(
                [
                    "docker-compose",
                    "-f", conf.name,
                    "down", "-v"
                ]
            )

def get_cluster():
    global CLUSTER, CLUSTER_CONF
    cluster_conf = {
        k: os.environ.get(k, CLUSTER_DEFAULTS.get(k))
        for k in ["ZOOKEEPER_PATH",
                  "ZOOKEEPER_CLASSPATH",
                  "ZOOKEEPER_PORT_OFFSET",
                  "ZOOKEEPER_CLUSTER_SIZE",
                  "ZOOKEEPER_VERSION",
                  "ZOOKEEPER_OBSERVER_START_ID",
                  "ZOOKEEPER_JAAS_AUTH"]
    }
    if CLUSTER is not None:
        if CLUSTER_CONF == cluster_conf:
            return CLUSTER
        else:
            log.info('Config change detected. Reconfiguring cluster...')
            CLUSTER.terminate()
            CLUSTER = None

    # Create a new cluster
    # ZK_HOME = cluster_conf.get("ZOOKEEPER_PATH")
    # ZK_CLASSPATH = cluster_conf.get("ZOOKEEPER_CLASSPATH")
    ZK_PORT_OFFSET = int(cluster_conf.get("ZOOKEEPER_PORT_OFFSET"))
    ZK_CLUSTER_SIZE = int(cluster_conf.get("ZOOKEEPER_CLUSTER_SIZE"))
    ZK_VERSION = cluster_conf.get("ZOOKEEPER_VERSION")
    if '-' in ZK_VERSION:
        # Ignore pre-release markers like -alpha
        ZK_VERSION = ZK_VERSION.split('-')[0]
    ZK_VERSION = tuple([int(n) for n in ZK_VERSION.split('.')])
    ZK_OBSERVER_START_ID = int(cluster_conf.get("ZOOKEEPER_OBSERVER_START_ID"))

    # assert ZK_HOME or ZK_CLASSPATH or ZK_VERSION, (
    #     "Either ZOOKEEPER_PATH or ZOOKEEPER_CLASSPATH or "
    #     "ZOOKEEPER_VERSION environment variable must be defined.\n"
    #     "For deb package installations this is /usr/share/java")

    ###########################################################################
    # Version specific configs
    if ZK_VERSION >= (3, 5):
        additional_configuration_entries = [
            "4lw.commands.whitelist=*",
            "reconfigEnabled=true"
        ]
        # If defined, this sets the superuser password to "test"
        additional_java_system_properties = [
            "-Dzookeeper.DigestAuthenticationProvider.superDigest="
            "super:D/InIHSb7yEEbrWz8b9l71RjZJU="
        ]
    else:
        additional_configuration_entries = []
        additional_java_system_properties = []

    ###########################################################################
    # JAAS config
    ZOOKEEPER_JAAS_AUTH = cluster_conf.get("ZOOKEEPER_JAAS_AUTH")
    if ZOOKEEPER_JAAS_AUTH == "digest":
        jaas_config = render(
            accounts=[
                {
                    "username": "super",
                    "password": "super_secret",
                },
                {
                    "username": "jaasuser",
                    "password": "jaas_password",
                }
            ]
        )
    elif ZOOKEEPER_JAAS_AUTH == "gssapi":
        # Configure Zookeeper to use our test KDC.
        additional_java_system_properties += [
            "-Djava.security.krb5.conf=%s" % os.path.expandvars(
                "${KRB5_CONFIG}"
            ),
            "-Dsun.security.krb5.debug=true",
        ]
        jaas_config = render(
            keytab_file=os.path.expandvars("${KRB5_TEST_ENV}/server.keytab"),
            principal="zookeeper/127.0.0.1@KAZOOTEST.ORG",
        )
    else:
        jaas_config = None


class ZookeeperCluster(object):
    
    def __init__(self, size=3, port_offset=20000, observer_start_id=-1,
                 configuration_entries=(),
                 java_system_properties=(),
                 jaas_config=None):
        self._install_path = install_path
        self._classpath = classpath
        self._servers = []


        # port_offset: each instance client is at port_offset + 10 * server_index.
        # from 0 -> observer_start_id: participant
        # from observer_start_id -> size: observer

    def __getitem__(self, k):
        """Should expose control object on instance k"""

    def __iter__(self):
        """Iterator over instances"""

    def start(self):
        """Start the cluster"""

    def stop(self):
        """Stop the cluster. (destructive?)"""

    def terminate(self):
        """Call destroy on instance?"""

    def reset(self):
        """Call reset on instance?"""


class Instance(object):
    def run(self):
        "Start the instance"
    
    @property
    def address(self):
        """Return host:port to instance client port"""
        # Do we want internal inside docker network or external forwarded port?

    @property
    def running(self) -> bool:
        """is it running?"""

    @property
    def client_port(self) -> int:
        """instance client port."""
        # Internal or mapped port?

    def stop(self):
        """Stop the instance, keep state."""

    def reset(self):
        """Stop instance, wipe state."""

    def destroy(self):
        """"Destroy intance, state, config, everything."""    