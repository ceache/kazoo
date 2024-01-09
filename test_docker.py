import pprint
import time
import docker

client = docker.from_env()

#print(client.containers.list())
#zoo1 = client.containers.get("zoo1")
#zkcli = zoo1.exec_run(
#        command="zkCli.sh -server zoo2",
#        stdin=True,
#        stdout=True,
#        stderr=False,
#        socket=True,
#        network="kazoo-harness",
#        auto_remove=True,
#        stream=True,
#        tty=True,
#        detach=True,
#        )

try:
    #zkcli = client.containers.create(
    #    image="zookeeper:3.7",
    #    command="zkCli.sh -server zoo2",
    #    network="kazoo-harness",
    #    auto_remove=True,
    #    stdin_open=True,
    #    tty=True,
    #)
    zkcli = client.containers.get("zkcli")
    s = zkcli.attach_socket(params={"stdin": True, "stdout": True, "stream": True})
    #zkcli.start()

    #time.sleep(5)
    #print(zkcli.logs())
    #zkcli.reload()

    #pprint.pprint(client.api.inspect_container(zkcli.id)['Config'])

    #print('res', s.read(1024*16))
    res = s._sock.send("""create /a ''
    create /b ''
    create /c ''
    create /d ''
    """.encode())
    print("sent", res)
    time.sleep(1)
    print('res', s.read(2048).decode())
    res = s._sock.send("ls /\n".encode())
    print("sent", res)
    time.sleep(1)
    print('res', s.read(2048).decode())

    #print("logs:", zkcli.logs().decode())


finally:
    #zkcli.kill()
    pass
