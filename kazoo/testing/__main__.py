from __future__ import annotations

import os

from jinja2 import Environment
from packaging import version


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

    env = Environment()
    with open("kazoo/testing/templates/docker-compose.yml.jinja") as fp:
        t = env.from_string(fp.read())

    os.makedirs(work_dir, exist_ok=True)
    with open(os.path.join(work_dir, "docker-compose.yml"), "w") as fp:
        fp.write(
            t.render(
                zookeeper_version=zk_version,
                num_servers=num_servers,
                num_participants=num_participants,
                use_kdc=use_kdc,
                zookeeper_work_dir="/tmp",
            )
        )


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("-z", "--zookeeper-version", required=True)
    parser.add_argument("-n", "--num-servers", required=True)
    parser.add_argument("-p", "--num-participants", default=None)
    parser.add_argument("-o", "--num-observers", default=None)
    parser.add_argument("-k", "--use-kdc", action="store_true", default=False)
    parser.add_argument("work_dir", metavar="WORKDIR")
    args = parser.parse_args(sys.argv[1:])

    zk_version = version.Version(args.zookeeper_version)
    num_servers = int(args.num_servers)
    num_participants = (
        int(args.num_participants) if args.num_participants else num_servers
    )
    num_observers = (
        int(args.num_observers)
        if args.num_observers
        else num_servers - num_participants
    )

    main(
        work_dir=args.work_dir,
        zookeeper_version=zk_version,
        num_servers=num_servers,
        num_participants=num_participants,
        num_observers=num_observers,
    )
