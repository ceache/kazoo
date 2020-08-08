#!/bin/bash

set -e
set -x

echo ###########################################################################
env
ls -l /
echo ###########################################################################

KRB5KDC=$(which krb5kdc)
KDB5_UTIL=$(which kdb5_util)
KADMIN=$(which kadmin.local)

WRK_DIR="/kdc-data"
KDC_DIR="${WRK_DIR}/krb5kdc"
LOG_DIR="${WRK_DIR}/logs"
KTB_DIR="${WRK_DIR}/keytabs"

mkdir -vp "${KDC_DIR}" "${LOG_DIR}" "${KTB_DIR}"

###############################################################################
# Cleanup handlers

function kdclogs {
    echo "Kerberos environment logs:"
    exec tail -v -n50 ${LOG_DIR}/*.log
}

trap kdclogs ERR

###############################################################################
export KRB5_CONFIG="${WRK_DIR}/krb5.conf"

KDC_PORT=${KDC_PORT-1088}
SPNS=${SPNS-"client server/localhost"}
REALM=${REALM-"EXAMPLE.ORG"}
DOMAIN=${DOMAIN-$(echo ${REALM} | tr '[:upper:]' '[:lower:]')}

cat <<EOF >"${KRB5_CONFIG}"
[logging]
 default = FILE:${LOG_DIR}/krb5libs.log
 kdc = FILE:${LOG_DIR}/krb5kdc.log
 admin_server = FILE:${LOG_DIR}/kadmind.log

[libdefaults]
 dns_lookup_realm = false
 ticket_lifetime = 24h
 renew_lifetime = 7d
 forwardable = true
 rdns = false
 default_realm = ${REALM}
 #default_ccache_name = KEYRING:persistent:%{uid}

[realms]
 ${REALM} = {
  database_name = ${KDC_DIR}/principal
  admin_keytab = FILE:${KDC_DIR}/kadm5.keytab
  key_stash_file = ${KDC_DIR}/stash
  kdc_listen = 127.0.0.1:${KDC_PORT}
  kdc_tcp_listen = 127.0.0.1:${KDC_PORT}
  kdc = 127.0.0.1:${KDC_PORT}
  default_domain = ${DOMAIN}
 }

[domain_realm]
 .${DOMAIN} = ${REALM}
 ${DOMAIN} = ${REALM}
EOF

cat <<EOF | ${KDB5_UTIL} create -s
passwd123
passwd123
EOF

for SPN in ${SPNS}; do
echo "add_principal -randkey ${SPN}@${REALM}"
echo "ktadd -k ${KTB_DIR}/${SPN//\//#}.keytab -norandkey ${SPN}@${REALM}"
done | ${KADMIN}

# Making keytabs world readable
find ${KTB_DIR} \
    -type f \
    -exec chmod go+r '{}' \;

# Starting KDC
echo "Starting KDC listening on ${KDC_PORT}..."
export KRB5_KDC_PROFILE=${KRB5_CONFIG}
exec ${KRB5KDC} \
    -P ${KDC_DIR}/kdc.pid \
    -p ${KDC_PORT} \
    -r ${REALM} \
    -n
