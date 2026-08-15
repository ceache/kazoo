To deploy a **3-node Apache ZooKeeper cluster** across **3 security configurations** (Plain/SASL Digest-MD5, TLS/mTLS, and GSSAPI/Kerberos) and **3 ZooKeeper versions** (`3.7.x`, `3.8.x`, `3.9.x`), you can structure the project using Docker Compose environment files (`.env`) or extension fields.

Because the official [`zookeeper`](https://hub.docker.com/_/zookeeper) image entrypoint standardizes configuration across 3.7+, the configuration mechanisms (such as `ZOO_MY_ID`, `ZOO_SERVERS`, `ZOO_CFG_EXTRA`, and `JVMFLAGS`) remain uniform across all 9 combinations.

---

### Project Structure Strategy

Organize the directory to select the security mode via subdirectories and the ZooKeeper version via `.env`:

```text
zookeeper-clusters/
├── plain-auth/
│   ├── .env                       # ZOOKEEPER_VERSION=3.9 (or 3.7 / 3.8)
│   ├── docker-compose.yml
│   └── conf/
│       └── jaas.conf
├── tls-auth/
│   ├── .env
│   ├── docker-compose.yml
│   └── certs/                     # Keystores & Truststores (JKS / PKCS12)
└── gssapi-kerberos/
    ├── .env
    ├── docker-compose.yml
    └── conf/
        ├── krb5.conf
        ├── jaas.conf
        └── keytabs/

```

---

### Matrix Configuration 1: Plain Auth (SASL Digest-MD5)

This setup enforces SASL Digest-MD5 authentication for client connections and inter-node quorum traffic.

#### `plain-auth/conf/jaas.conf`

```properties
Server {
    org.apache.zookeeper.server.auth.DigestLoginModule required
    user_admin="admin-secret"
    user_client="client-secret";
};

Client {
    org.apache.zookeeper.server.auth.DigestLoginModule required
    username="admin"
    password="admin-secret";
};

```

#### `plain-auth/docker-compose.yml`

```yaml
version: '3.8'

x-zk-common: &zk-common
  image: zookeeper:${ZOOKEEPER_VERSION:-3.9}
  restart: always
  volumes:
    - ./conf/jaas.conf:/conf/jaas.conf:ro
  environment:
    ZOO_SERVERS: server.1=zoo1:2888:3888;2181 server.2=zoo2:2888:3888;2181 server.3=zoo3:2888:3888;2181
    JVMFLAGS: "-Djava.security.auth.login.config=/conf/jaas.conf"
    ZOO_CFG_EXTRA: |
      authProvider.1=org.apache.zookeeper.server.auth.SASLAuthenticationProvider
      requireClientAuthScheme=sasl
      quorum.auth.enableSasl=true
      quorum.auth.learnerRequireSasl=true
      quorum.auth.serverRequireSasl=true
      quorum.auth.learner.saslLoginContext=Client
      quorum.auth.server.saslLoginContext=Server

networks:
  zk-net:
    driver: bridge

services:
  zoo1:
    <<: *zk-common
    container_name: zk-plain-1
    environment:
      <<: *zk-common-env
      ZOO_MY_ID: 1
    ports:
      - "2181:2181"
    networks:
      - zk-net

  zoo2:
    <<: *zk-common
    container_name: zk-plain-2
    environment:
      <<: *zk-common-env
      ZOO_MY_ID: 2
    ports:
      - "2182:2181"
    networks:
      - zk-net

  zoo3:
    <<: *zk-common
    container_name: zk-plain-3
    environment:
      <<: *zk-common-env
      ZOO_MY_ID: 3
    ports:
      - "2183:2181"
    networks:
      - zk-net

```

---

### Matrix Configuration 2: TLS / mTLS (Client & Quorum TLS)

ZooKeeper uses Netty as the underlying network communication framework for SSL/TLS. This configuration enables mutual TLS for client connections on port `2182` and encrypts internal quorum communication.

#### Generate Certificates (Run inside `tls-auth/certs/`)

```bash
# Generate CA, Keystore, and Truststore
keytool -genkeypair -alias zk-ca -dname "CN=ZK-CA" -keystore truststore.jks -storepass changeit -keypass changeit -validity 365 -keyalg RSA
keytool -genkeypair -alias zoo1 -dname "CN=zoo1" -keystore server.jks -storepass changeit -keypass changeit -validity 365 -keyalg RSA
# Export and import certs into truststore...

```

#### `tls-auth/docker-compose.yml`

```yaml
version: '3.8'

x-zk-common: &zk-common
  image: zookeeper:${ZOOKEEPER_VERSION:-3.9}
  restart: always
  volumes:
    - ./certs:/certs:ro
  environment:
    ZOO_SERVERS: server.1=zoo1:2888:3888;2181 server.2=zoo2:2888:3888;2181 server.3=zoo3:2888:3888;2181
    ZOO_CFG_EXTRA: |
      serverCnxnFactory=org.apache.zookeeper.server.NettyServerCnxnFactory
      secureClientPort=2182
      ssl.keyStore.location=/certs/server.jks
      ssl.keyStore.password=changeit
      ssl.trustStore.location=/certs/truststore.jks
      ssl.trustStore.password=changeit
      ssl.clientAuth=need
      
      # Quorum TLS Settings
      ssl.quorum.keyStore.location=/certs/server.jks
      ssl.quorum.keyStore.password=changeit
      ssl.quorum.trustStore.location=/certs/truststore.jks
      ssl.quorum.trustStore.password=changeit
      ssl.quorum.enabled=true
      ssl.quorum.hostnameVerification=false

networks:
  zk-net:
    driver: bridge

services:
  zoo1:
    <<: *zk-common
    container_name: zk-tls-1
    environment:
      <<: *zk-common-env
      ZOO_MY_ID: 1
    ports:
      - "2181:2181"
      - "2281:2182"
    networks:
      - zk-net

  zoo2:
    <<: *zk-common
    container_name: zk-tls-2
    environment:
      <<: *zk-common-env
      ZOO_MY_ID: 2
    ports:
      - "2182:2181"
      - "2282:2182"
    networks:
      - zk-net

  zoo3:
    <<: *zk-common
    container_name: zk-tls-3
    environment:
      <<: *zk-common-env
      ZOO_MY_ID: 3
    ports:
      - "2183:2181"
      - "2283:2182"
    networks:
      - zk-net

```

---

### Matrix Configuration 3: GSSAPI / Kerberos Auth

For GSSAPI, a dedicated MIT Kerberos KDC container provides ticket granting, and JAAS context maps ZooKeeper service principals.

#### `gssapi-kerberos/conf/krb5.conf`

```ini
[libdefaults]
    default_realm = EXAMPLE.COM
    dns_lookup_realm = false
    dns_lookup_kdc = false
    ticket_lifetime = 24h
    forwardable = true

[realms]
    EXAMPLE.COM = {
        kdc = kdc:88
        admin_server = kdc:749
    }

[domain_realm]
    .example.com = EXAMPLE.COM
    example.com = EXAMPLE.COM

```

#### `gssapi-kerberos/conf/jaas.conf`

```properties
Server {
    com.sun.security.auth.module.Krb5LoginModule required
    useKeyTab=true
    keyTab="/conf/keytabs/zookeeper.keytab"
    storeKey=true
    useTicketCache=false
    principal="zookeeper/zoo1.example.com@EXAMPLE.COM";
};

```

#### `gssapi-kerberos/docker-compose.yml`

```yaml
version: '3.8'

networks:
  zk-net:
    driver: bridge

services:
  kdc:
    image: gcavalcante8808/krb5-kdc
    container_name: kdc
    environment:
      REALM: EXAMPLE.COM
      KDC_KADMIN_DEF_PASS: adminpassword
    volumes:
      - ./conf/keytabs:/etc/security/keytabs
    networks:
      - zk-net

  zoo1:
    image: zookeeper:${ZOOKEEPER_VERSION:-3.9}
    container_name: zk-gssapi-1
    restart: always
    depends_on:
      - kdc
    volumes:
      - ./conf/jaas.conf:/conf/jaas.conf:ro
      - ./conf/krb5.conf:/etc/krb5.conf:ro
      - ./conf/keytabs:/conf/keytabs:ro
    environment:
      ZOO_MY_ID: 1
      ZOO_SERVERS: server.1=zoo1:2888:3888;2181 server.2=zoo2:2888:3888;2181 server.3=zoo3:2888:3888;2181
      JVMFLAGS: "-Djava.security.auth.login.config=/conf/jaas.conf -Djava.security.krb5.conf=/etc/krb5.conf -Dsun.security.krb5.debug=true"
      ZOO_CFG_EXTRA: |
        authProvider.1=org.apache.zookeeper.server.auth.SASLAuthenticationProvider
        requireClientAuthScheme=sasl
    ports:
      - "2181:2181"
    networks:
      - zk-net

```

---

### Target Matrix Run Script

To spin up any specific version across the target matrix, pass `ZOOKEEPER_VERSION`:

```bash
# Run ZooKeeper 3.7.x with Plain SASL Auth
ZOOKEEPER_VERSION=3.7 docker-compose -f plain-auth/docker-compose.yml up -d

# Run ZooKeeper 3.8.x with TLS
ZOOKEEPER_VERSION=3.8 docker-compose -f tls-auth/docker-compose.yml up -d

# Run ZooKeeper 3.9.x with Kerberos GSSAPI
ZOOKEEPER_VERSION=3.9 docker-compose -f gssapi-kerberos/docker-compose.yml up -d

```

---

### Key Behavioral Differences Across 3.7 / 3.8 / 3.9

1. **Netty Defaulting:** 3.8+ defaults to Netty 4.1.x and stricter cipher suite selections for TLS.
2. **AdminServer:** Embedded Jetty AdminServer (port 8080) is enabled by default across all three versions.
3. **Quorum Encryption:** 3.9 includes enhanced SSL renegotiation handling under high network throughput.



To achieve a clean, modular setup using Docker Compose's native file-merging engine (`docker compose -f base.yml -f overlay.yml ...`), we split configurations into **Orthogonal Overlays**:

1. **`docker-compose.base.yml`**: Official ZooKeeper container with standard unauthenticated ports, parameterizable version, and 4-letter-word healthchecks.
2. **Auth Overlays**: Spin up supporting sidecars/init containers (KDC, OpenSSL cert-gen) and inject JAAS or Netty TLS configurations via shared volumes.
3. **Feature Overlays**: Toggle extended engine features (TTL, Read-Only, Reconfig) via JVM flags and `ZOO_CFG_EXTRA`.

---

### File Structure

```text
tests/compose/
├── docker-compose.base.yml
├── docker-compose.auth-digest.yml
├── docker-compose.auth-gssapi.yml
├── docker-compose.auth-tls.yml
├── docker-compose.feat-ttl.yml
└── docker-compose.feat-readonly.yml

```

---

### 1. Base Configuration (`docker-compose.base.yml`)

Uses official environment variables from Docker Hub's `library/zookeeper`:

```yaml
# tests/compose/docker-compose.base.yml
services:
  zookeeper:
    image: zookeeper:${ZK_VERSION:-3.9}
    container_name: zk-ensemble
    environment:
      ZOO_4LW_COMMANDS_WHITELIST: "ruok,srvr,stat,mntr,conf,isro"
      ZOO_ADMINSERVER_ENABLED: "false"
      ZOO_STANDALONE_ENABLED: "true"
    ports:
      - "${ZK_PORT:-2181}:2181"
    healthcheck:
      test: ["CMD-SHELL", "echo ruok | nc -w 2 127.0.0.1 2181 | grep imok"]
      interval: 3s
      timeout: 3s
      retries: 15
      start_period: 5s
    networks:
      - zk-net

networks:
  zk-net:
    driver: bridge

```

---

### 2. Authentication Overlays

#### A. SASL + Digest-MD5 (`docker-compose.auth-digest.yml`)

Generates an inline `jaas.conf` file via a lightweight init container and injects the JAAS JVM property.

```yaml
# tests/compose/docker-compose.auth-digest.yml
services:
  jaas-digest-init:
    image: alpine:latest
    container_name: zk-jaas-digest-init
    volumes:
      - jaas-digest-data:/conf
    command:
      - /bin/sh
      - -c
      - |
        cat <<'EOF' > /conf/jaas.conf
        Server {
            org.apache.zookeeper.server.auth.DigestLoginModule required
            user_super="adminsecret"
            user_kazoo="testsecret";
        };
        EOF
        chmod 644 /conf/jaas.conf
    networks:
      - zk-net

  zookeeper:
    depends_on:
      jaas-digest-init:
        condition: service_completed_successfully
    volumes:
      - jaas-digest-data:/conf/auth
    environment:
      JVMFLAGS: "-Djava.security.auth.login.config=/conf/auth/jaas.conf"
      ZOO_CFG_EXTRA: |
        authProvider.1=org.apache.zookeeper.server.auth.SASLAuthenticationProvider
        requireClientAuthScheme=sasl

volumes:
  jaas-digest-data:

```

---

#### B. SASL + Kerberos / GSSAPI (`docker-compose.auth-gssapi.yml`)

Launches an **MIT Kerberos KDC** container, creates principals (`zookeeper/zk-ensemble@EXAMPLE.COM`, `kazoo@EXAMPLE.COM`), exports keytabs to a shared volume, and configures ZooKeeper's JAAS module.

```yaml
# tests/compose/docker-compose.auth-gssapi.yml
services:
  kdc:
    image: debian:bookworm-slim
    container_name: zk-kdc
    environment:
      KRB5_REALM: EXAMPLE.COM
      KRB5_KDC: kdc
    volumes:
      - krb5-data:/etc/krb5-shared
    networks:
      zk-net:
        aliases:
          - kdc.example.com
    command:
      - /bin/bash
      - -c
      - |
        set -e
        apt-get update && apt-get install -y krb5-kdc krb5-admin-server
        
        # 1. Configure Realm
        cat <<'EOF' > /etc/krb5.conf
        [libdefaults]
            default_realm = EXAMPLE.COM
            dns_lookup_realm = false
            dns_lookup_kdc = false
            ticket_lifetime = 24h
            forwardable = true

        [realms]
            EXAMPLE.COM = {
                kdc = kdc
                admin_server = kdc
            }
        EOF
        cp /etc/krb5.conf /etc/krb5-shared/krb5.conf

        # 2. Initialize Database & Principals
        kdb5_util create -s -P adminpassword
        kadmin.local -q "addprinc -randkey zookeeper/zk-ensemble@EXAMPLE.COM"
        kadmin.local -q "addprinc -randkey kazoo@EXAMPLE.COM"
        kadmin.local -q "ktadd -k /etc/krb5-shared/zookeeper.keytab zookeeper/zk-ensemble@EXAMPLE.COM"
        kadmin.local -q "ktadd -k /etc/krb5-shared/kazoo.keytab kazoo@EXAMPLE.COM"
        chmod 644 /etc/krb5-shared/*.keytab /etc/krb5-shared/krb5.conf

        # 3. Create Server JAAS config
        cat <<'EOF' > /etc/krb5-shared/jaas.conf
        Server {
            com.sun.security.auth.module.Krb5LoginModule required
            useKeyTab=true
            keyTab="/conf/auth/zookeeper.keytab"
            storeKey=true
            useTicketCache=false
            principal="zookeeper/zk-ensemble@EXAMPLE.COM";
        };
        EOF

        # 4. Start KDC daemon
        echo "KDC Ready"
        exec /usr/sbin/krb5kdc -n
    healthcheck:
      test: ["CMD-SHELL", "test -f /etc/krb5-shared/zookeeper.keytab && test -f /etc/krb5-shared/jaas.conf"]
      interval: 2s
      timeout: 2s
      retries: 15

  zookeeper:
    depends_on:
      kdc:
        condition: service_healthy
    volumes:
      - krb5-data:/conf/auth
    environment:
      JVMFLAGS: "-Djava.security.auth.login.config=/conf/auth/jaas.conf -Djava.security.krb5.conf=/conf/auth/krb5.conf -Dsun.security.krb5.debug=true"
      ZOO_CFG_EXTRA: |
        authProvider.1=org.apache.zookeeper.server.auth.SASLAuthenticationProvider
        jaasLoginRenew=3600000

volumes:
  krb5-data:

```

---

#### C. Mutual TLS (mTLS) via Netty (`docker-compose.auth-tls.yml`)

Uses an ephemeral container with `openssl` + Java `keytool` to generate a self-signed Root CA, server PKCS12 keystore/truststore, and exportable client certificates for Kazoo.

```yaml
# tests/compose/docker-compose.auth-tls.yml
services:
  certgen:
    image: eclipse-temurin:17-jdk-jammy
    container_name: zk-certgen
    volumes:
      - tls-certs:/certs
    command:
      - /bin/bash
      - -c
      - |
        set -e
        apt-get update && apt-get install -y openssl
        cd /certs

        # 1. Root CA
        openssl req -x509 -newkey rsa:2048 -days 365 -nodes \
          -keyout ca.key -out ca.crt -subj "/CN=TestZKCA"

        # 2. Server Certificate
        openssl req -newkey rsa:2048 -nodes \
          -keyout server.key -out server.csr -subj "/CN=zk-ensemble"
        openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key \
          -CAcreateserial -out server.crt -days 365

        # 3. Server PKCS12 Keystore & Truststore for ZooKeeper Netty
        openssl pkcs12 -export -in server.crt -inkey server.key \
          -out server.keystore.p12 -name zookeeper -passout pass:changeit
        keytool -import -trustcacerts -noprompt -alias ca \
          -file ca.crt -keystore server.truststore.p12 -storetype PKCS12 -storepass changeit

        # 4. Client Certificate for Kazoo Client
        openssl req -newkey rsa:2048 -nodes \
          -keyout client.key -out client.csr -subj "/CN=kazoo-client"
        openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key \
          -CAcreateserial -out client.crt -days 365

        chmod 644 /certs/*
    healthcheck:
      test: ["CMD-SHELL", "test -f /certs/server.keystore.p12 && test -f /certs/client.crt"]
      interval: 2s
      timeout: 2s
      retries: 15

  zookeeper:
    depends_on:
      certgen:
        condition: service_completed_successfully
    ports:
      - "${ZK_TLS_PORT:-2281}:2281"
    volumes:
      - tls-certs:/certs
    environment:
      ZOO_CFG_EXTRA: |
        secureClientPort=2281
        serverCnxnFactory=org.apache.zookeeper.server.NettyServerCnxnFactory
        ssl.keyStore.location=/certs/server.keystore.p12
        ssl.keyStore.password=changeit
        ssl.keyStore.type=PKCS12
        ssl.trustStore.location=/certs/server.truststore.p12
        ssl.trustStore.password=changeit
        ssl.trustStore.type=PKCS12
        ssl.clientAuth=need
        authProvider.1=org.apache.zookeeper.server.auth.X509AuthenticationProvider

volumes:
  tls-certs:

```

---

### 3. Feature Overlays

#### A. Extended Types & TTL Nodes (`docker-compose.feat-ttl.yml`)

Enables extended node types and TTL node emulation.

```yaml
# tests/compose/docker-compose.feat-ttl.yml
services:
  zookeeper:
    environment:
      JVMFLAGS: "-Dzookeeper.extendedTypesEnabled=true -Dzookeeper.emulate353TTLNodes=true"

```

#### B. Read-Only Mode (`docker-compose.feat-readonly.yml`)

Enables read-only server mode for network partition/degraded testing.

```yaml
# tests/compose/docker-compose.feat-readonly.yml
services:
  zookeeper:
    environment:
      ZOO_CFG_EXTRA: |
        readonlymode.enabled=true

```

---

### 4. How Docker Merges These Files

Docker Compose merges configurations according to standard rules:

* **`volumes` & `ports**`: Appended/concatenated.
* **`environment`**: Map merged by key.

> **Important on `JVMFLAGS` and `ZOO_CFG_EXTRA`:** Since environment variables with the same key override previous values entirely, if you combine an auth overlay (which sets `JVMFLAGS` for JAAS) with a feature overlay (which sets `JVMFLAGS` for TTL), pass combined flags or use `ZOO_CFG_EXTRA` where possible.

#### Manual CLI Verification

```bash
# 1. Plain ZK 3.8 with TTL Features
ZK_VERSION=3.8 docker compose \
  -f tests/compose/docker-compose.base.yml \
  -f tests/compose/docker-compose.feat-ttl.yml \
  up -d --wait

# 2. ZK 3.9 with Full Kerberos KDC
ZK_VERSION=3.9 docker compose \
  -f tests/compose/docker-compose.base.yml \
  -f tests/compose/docker-compose.auth-gssapi.yml \
  up -d --wait

# 3. ZK 3.9 with mTLS (Netty)
ZK_VERSION=3.9 docker compose \
  -f tests/compose/docker-compose.base.yml \
  -f tests/compose/docker-compose.auth-tls.yml \
  up -d --wait

```

---

### 5. Programmatic Pytest Integration (`testcontainers-python`)

In your session fixture, dynamically supply the list of compose files based on the requested pytest CLI flags:

```python
# conftest.py snippet
import os
import pytest
from testcontainers.compose import DockerCompose

@pytest.fixture(scope="session")
def docker_env(request):
    zk_version = request.config.getoption("--zk-version")
    zk_auth = request.config.getoption("--zk-auth")         # plain, digest, sasl_gssapi, tls
    zk_features = request.config.getoption("--zk-features") # e.g. "ttl", "readonly"

    compose_dir = os.path.abspath("tests/compose")
    compose_files = ["docker-compose.base.yml"]

    # 1. Attach Auth Overlay
    if zk_auth == "digest":
        compose_files.append("docker-compose.auth-digest.yml")
    elif zk_auth == "sasl_gssapi":
        compose_files.append("docker-compose.auth-gssapi.yml")
    elif zk_auth == "tls":
        compose_files.append("docker-compose.auth-tls.yml")

    # 2. Attach Feature Overlays
    features_set = set(zk_features.split(",")) if zk_features else set()
    if "ttl" in features_set:
        compose_files.append("docker-compose.feat-ttl.yml")
    if "readonly" in features_set:
        compose_files.append("docker-compose.feat-readonly.yml")

    # 3. Run Stack
    with DockerCompose(
        context=compose_dir,
        compose_file_name=compose_files,
        env_vars={"ZK_VERSION": zk_version}
    ) as compose:
        # Determine client port (2281 if TLS, otherwise 2181)
        target_port = 2281 if zk_auth == "tls" else 2181
        host = compose.get_service_host("zookeeper", target_port)
        port = compose.get_service_port("zookeeper", target_port)

        yield KazooZkEnv(
            version=zk_version,
            auth=zk_auth,
            features=features_set,
            hosts=f"{host}:{port}",
        )

```
