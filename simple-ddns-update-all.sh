#!/bin/bash


CONFIG_PATH=$1
IP_VERSION=$2
TRIGGER_NAME=$3
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")

for conf_file in "$CONFIG_PATH"/*_ipv"$IP_VERSION"; do
  conf_base_name=$(basename "$conf_file")
  "$SCRIPT_DIR"/simple-ddns.sh --config_dir="$CONFIG_PATH" --host "$conf_base_name" --trigger "$TRIGGER_NAME"
done

