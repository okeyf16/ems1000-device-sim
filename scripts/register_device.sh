#!/usr/bin/env bash
# Register a simulated device in IoT Hub and write its connection string to .env.
# Usage: scripts/register_device.sh <iot-hub-name> [device-id] [resource-group]
# Needs: Azure CLI logged in (az login). Installs the azure-iot extension if missing.
set -euo pipefail

HUB="${1:?usage: $0 <iot-hub-name> [device-id] [resource-group]}"
DEVICE="${2:-ems-sim-01}"
RG_ARGS=()
[[ -n "${3:-}" ]] && RG_ARGS=(--resource-group "$3")

az extension show --name azure-iot >/dev/null 2>&1 || az extension add --name azure-iot --only-show-errors

if az iot hub device-identity show --hub-name "$HUB" --device-id "$DEVICE" "${RG_ARGS[@]}" >/dev/null 2>&1; then
  echo "Device '$DEVICE' already exists in '$HUB' - reusing it."
else
  az iot hub device-identity create --hub-name "$HUB" --device-id "$DEVICE" \
    --auth-method shared_private_key "${RG_ARGS[@]}" --only-show-errors >/dev/null
  echo "Created device '$DEVICE' in '$HUB'."
fi

CONN=$(az iot hub device-identity connection-string show --hub-name "$HUB" \
  --device-id "$DEVICE" "${RG_ARGS[@]}" --query connectionString -o tsv)

VAR="IOTHUB_CONN_$(echo "$DEVICE" | tr '[:lower:]-' '[:upper:]_')"
touch .env
grep -v "^${VAR}=" .env > .env.tmp || true
echo "${VAR}=${CONN}" >> .env.tmp
mv .env.tmp .env
chmod 600 .env
echo "Wrote ${VAR} to .env (not committed - see .gitignore)."
