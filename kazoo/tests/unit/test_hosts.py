from kazoo import hosts as kazoo_hosts


def test_ipv4():
    """Test both string and list of strings for IPv4 hosts."""
    hosts, chroot = kazoo_hosts.collect_hosts(
        "127.0.0.1:2181, 192.168.1.2:2181, 132.254.111.10:2181"
    )
    assert hosts == [
        ("127.0.0.1", 2181),
        ("192.168.1.2", 2181),
        ("132.254.111.10", 2181),
    ]
    assert chroot is None

    hosts, chroot = kazoo_hosts.collect_hosts(
        ["127.0.0.1:2181", "192.168.1.2:2181", "132.254.111.10:2181"]
    )
    assert hosts == [
        ("127.0.0.1", 2181),
        ("192.168.1.2", 2181),
        ("132.254.111.10", 2181),
    ]
    assert chroot is None


def test_ipv6():
    """Test both string and list of strings for IPv6 hosts."""
    hosts, chroot = kazoo_hosts.collect_hosts(
        "[fe80::200:5aee:feaa:20a2]:2181"
    )
    assert hosts == [("fe80::200:5aee:feaa:20a2", 2181)]
    assert chroot is None

    hosts, chroot = kazoo_hosts.collect_hosts(
        ["[fe80::200:5aee:feaa:20a2]:2181"]
    )
    assert hosts == [("fe80::200:5aee:feaa:20a2", 2181)]
    assert chroot is None


def test_hosts_list():
    """Test various host list formats, including with a chroot path."""
    hosts, chroot = kazoo_hosts.collect_hosts(
        "zk01:2181, zk02:2181, zk03:2181"
    )
    expected_hosts = [("zk01", 2181), ("zk02", 2181), ("zk03", 2181)]
    assert hosts == expected_hosts
    assert chroot is None

    hosts, chroot = kazoo_hosts.collect_hosts(
        ["zk01:2181", "zk02:2181", "zk03:2181"]
    )
    assert hosts == expected_hosts
    assert chroot is None

    expected_chroot = "/test"
    hosts, chroot = kazoo_hosts.collect_hosts(
        "zk01:2181, zk02:2181, zk03:2181/test"
    )
    assert hosts == expected_hosts
    assert chroot == expected_chroot

    hosts, chroot = kazoo_hosts.collect_hosts(
        ["zk01:2181", "zk02:2181", "zk03:2181", "/test"]
    )
    assert hosts == expected_hosts
    assert chroot == expected_chroot
