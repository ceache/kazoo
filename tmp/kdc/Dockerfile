FROM debian:buster-slim

ENV DEBIAN_FRONTEND noninteractive

RUN \
    apt update && \
    apt dist-upgrade && \
    apt install -y krb5-admin-server krb5-kdc && \
    apt autoremove && \
    apt clean && \
    rm -rf \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*

COPY --chown=daemon:daemon /root /

USER daemon
VOLUME /kdc-data
ENV SPNS "client server/localhost"
ENV REALM "EXAMPLE.ORG"
EXPOSE 1088

ENTRYPOINT [ "/entrypoint.sh" ]
