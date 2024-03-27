#!/bin/sh

set -x
set -e

HERE=$(cd $(dirname $0) && pwd -P)

ZOOKEEPER_VERSION=${ZOOKEEPER_VERSION:-3.9.2}
ZOOKEEPER_PATH=${ZOOKEEPER_PATH:-"${HERE}/zookeeper/${ZOOKEEPER_VERSION}"}


download_zookeeper() {
    ZOOKEEPER_PREFIX=${ZOOKEEPER_PREFIX-"apache-"}
    ZOOKEEPER_SUFFIX=${ZOOKEEPER_SUFFIX-"-bin"}
    ZOOKEEPER_LIB=${ZOOKEEPER_LIB-"lib"}
    ZOO_MIRROR_URL="https://archive.apache.org/dist"
    ZOOKEEPER_DOWNLOAD_URL=${ZOO_MIRROR_URL}/zookeeper/zookeeper-${ZOOKEEPER_VERSION}/${ZOOKEEPER_PREFIX}zookeeper-${ZOOKEEPER_VERSION}${ZOOKEEPER_SUFFIX}.tar.gz
    ZOOKEEPER_ARCHIVE="${ZOOKEEPER_PREFIX}zookeeper-${ZOOKEEPER_VERSION}${ZOOKEEPER_SUFFIX}"
    ZOO_BASE_DIR="${HERE}/zookeeper"

    mkdir -p ${ZOO_BASE_DIR}
    echo "Will download ZK from ${ZOOKEEPER_DOWNLOAD_URL}"
    (
        curl --silent -L -C - $ZOOKEEPER_DOWNLOAD_URL \
            | tar -zx -C "${ZOO_BASE_DIR}"
    ) || (echo "Failed downloading ZK from ${ZOOKEEPER_DOWNLOAD_URL}" && exit 1)
    mv -v "${ZOO_BASE_DIR}/${ZOOKEEPER_ARCHIVE}" "${ZOOKEEPER_PATH}"
    chmod a+x "${ZOOKEEPER_PATH}/bin/zkServer.sh"
}

if [ ! -d "${ZOOKEEPER_PATH}" ]; then
    download_zookeeper
    echo "Downloaded zookeeper ${ZOOKEEPER_VERSION} to ${ZOOKEEPER_PATH}"
else
    echo "Already downloaded zookeeper ${ZOOKEEPER_VERSION} to ${ZOOKEEPER_PATH}"
fi

export ZOOKEEPER_VERSION
# Used as install_path when starting ZK
export ZOOKEEPER_PATH

# Yield execution to venv command
exec $*
