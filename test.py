from __future__ import annotations

import subprocess
from jinja2 import Environment


def main():
    env = Environment()
    with open ("kazoo/testing/templates/docker-compose.yml.jinja") as fp:
        t = env.from_string(fp.read())

    print(
        t.render(
            zookeeper_version="3.6",
            num_servers=3,
            use_kdc=False,
            zookeeper_work_dir="/tmp",
        )
    )

if __name__ == "__main__":
    main()
