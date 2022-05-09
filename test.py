from __future__ import annotations

import time
import tempfile
import subprocess

from jinja2 import Environment


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

if __name__ == "__main__":
    main()
