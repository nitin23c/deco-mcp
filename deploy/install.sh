#!/bin/sh
# install.sh: install or update deco-mcp on a Debian or Ubuntu host (a small
# LXC or VM is ideal). Run as root from a checkout of this repo:
#
#   sudo ./deploy/install.sh
#
# Installs: python3-venv and git (apt), uv (pip, in /opt/uv-tool), Python 3.14
# (uv, in /opt/deco-mcp/python), the pinned library with hashes, this package,
# a deco-mcp system user and the systemd unit. Does not start the service.
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST=/opt/deco-mcp
UV=/opt/uv-tool/bin/uv
export DEBIAN_FRONTEND=noninteractive UV_PYTHON_INSTALL_DIR=$DEST/python

if ! command -v git >/dev/null || ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  apt-get update -qq
  apt-get install -y -qq python3-venv git ca-certificates >/dev/null
fi
[ -x "$UV" ] || { python3 -m venv /opt/uv-tool && /opt/uv-tool/bin/pip install -q uv; }
id deco-mcp >/dev/null 2>&1 || useradd --system --home-dir "$DEST" --shell /usr/sbin/nologin deco-mcp

mkdir -p "$DEST"
[ -x "$DEST/venv/bin/python" ] || "$UV" venv -q --python 3.14 "$DEST/venv"
"$UV" pip compile -q "$SRC/deploy/requirements.in" -o "$DEST/requirements.txt" \
  --python "$DEST/venv/bin/python" --generate-hashes
"$UV" pip sync -q --python "$DEST/venv/bin/python" "$DEST/requirements.txt"
"$UV" pip install -q --no-deps --reinstall --python "$DEST/venv/bin/python" "$SRC"

install -m 0755 "$SRC/deploy/deco-mcp-setup" /usr/local/sbin/deco-mcp-setup
install -m 0644 "$SRC/deploy/deco-mcp.service" /etc/systemd/system/deco-mcp.service
chown -R root:root "$DEST"
chmod -R go-w,o+rX "$DEST"
systemctl daemon-reload
systemctl enable deco-mcp >/dev/null 2>&1

"$DEST/venv/bin/python" -I -m deco_mcp --version
if [ -f /etc/deco-mcp.env ]; then
  echo "Installed. Restart to apply: systemctl restart deco-mcp"
else
  echo "Installed. Next: deco-mcp-setup, then systemctl start deco-mcp"
fi
