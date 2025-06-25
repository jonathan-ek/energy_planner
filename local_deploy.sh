#!/bin/bash
PLUGIN_DIR="energy_planner"
REPO_DIR="$(dirname "$0")"
FILES_DIR="${REPO_DIR}/custom_components/energy_planner"
HA_DIR="/home/jonathan/ha_demo"
CONFIG_DIR="/home/jonathan/ha_demo/config"
DEST_DIR="${CONFIG_DIR}/custom_components/"

echo "Copying files from $FILES_DIR to $DEST_DIR"
mkdir -p "${DEST_DIR}${PLUGIN_DIR}"
cp -r "$FILES_DIR" "$DEST_DIR"
cp "$REPO_DIR/energy_planner_extras.yaml" "$CONFIG_DIR/energy_planner_extras.yaml"

pushd "$HA_DIR" || exit
echo "Restarting Home Assistant"
if ! docker compose restart; then
    echo "Failed to restart Home Assistant. Please check the logs."
    exit 1
fi
echo "Home Assistant restarted successfully."
popd || exit
echo "Deployment completed successfully."