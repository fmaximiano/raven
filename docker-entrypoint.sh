#!/bin/sh
# Ajusta a posse do volume de dados (Railway e outros hosts montam volumes como root)
# e executa o RAVEN sem privilégios.
set -e
DATA_DIR="${RAVEN_DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA_DIR"
  chown -R raven:raven "$DATA_DIR"
  exec setpriv --reuid=raven --regid=raven --init-groups "$@"
fi
exec "$@"
