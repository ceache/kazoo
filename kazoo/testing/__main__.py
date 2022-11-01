from __future__ import annotations

import os

from jinja2 import Environment
from packaging import version, specifiers

_TEMPLATE = "kazoo/testing/templates"


def _render(target_dir, template_name, **kw_args):
    assert template_name.endswith(".jinja")
    template_res = os.path.basename(template_name)[: -len(".jinja")]

    env = Environment()
    env.filters["as_specifier"] = specifiers.SpecifierSet

    with open(os.path.join(_TEMPLATE, template_name)) as fp:
        t = env.from_string(fp.read())

    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(target_dir, template_res), "w") as fp:
        fp.write(t.render(**kw_args))


def main(
    work_dir: str,
    zookeeper_version: version.Version,
    num_servers: int,
    num_participants: int,
    num_observers: int,
    use_kdc=False,
):
    if num_participants is None:
        num_participants = num_servers
    assert num_servers >= num_participants + num_observers

    print(
        "DEBUG:",
        repr([work_dir, zk_version, num_servers, num_participants, use_kdc]),
    )

    _render(
        work_dir,
        "docker-compose.yml.jinja",
        zookeeper_version=zk_version,
        num_servers=num_servers,
        num_participants=num_participants,
        num_observers=num_observers,
        use_kdc=use_kdc,
        zookeeper_work_dir="/tmp",
        zookeeper_extra_opts={
            "reconfigEnabled": "true",
        },
        zookeeper_extra_jvmflags={
            "readonlymode.enabled": "true",
            "zookeeper.extendedTypesEnabled": "true",
            "zookeeper.DigestAuthenticationProvider.superDigest":
            "super:D/InIHSb7yEEbrWz8b9l71RjZJU=",
        },
    )

    for i in range(num_servers):
        # _render(
        #     os.path.join(work_dir, f"zoo{i}"),
        #     "zoo.cfg.jinja",
        #     zookeeper_data_dir="/tmp/zoo1",
        #     zookeeper_version=zk_version,
        #     zookeeper_client_port=2880,
        #     zookeeper_admin_port=2890,
        # )
        _render(
            os.path.join(work_dir, f"zoo{i}"),
            "jaas.conf.jinja",
        )


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("-z", "--zookeeper-version", required=True)
    parser.add_argument("-n", "--num-servers", required=True)
    parser.add_argument("-p", "--num-participants", default=None)
    parser.add_argument("-o", "--num-observers", default=0)
    parser.add_argument("-k", "--use-kdc", action="store_true", default=False)
    parser.add_argument("work_dir", metavar="WORKDIR")
    args = parser.parse_args(sys.argv[1:])

    zk_version = version.Version(args.zookeeper_version)
    num_servers = int(args.num_servers)
    num_participants = (
        int(args.num_participants) if args.num_participants else num_servers
    )
    num_observers = int(args.num_observers)

    main(
        work_dir=args.work_dir,
        zookeeper_version=zk_version,
        num_servers=num_servers,
        num_participants=num_participants,
        num_observers=num_observers,
    )
