#!/bin/bash
PLUGIN_DIR="energy_planner"
FILES_DIR="$(dirname "$0")/custom_components/energy_planner"
DEST_DIR="/home/jonathan/ha_demo/config/custom_components/"

echo "Copying files from $FILES_DIR to $DEST_DIR"
mkdir -p "${DEST_DIR}${PLUGIN_DIR}"
cp -r "$FILES_DIR" "$DEST_DIR"