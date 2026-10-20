#!/usr/bin/env bash
# On hwe-bench, from the mirror: start.sh NAME -> detached harden with overlay NAME.json, pid in build/sw-NAME.pid
set -euo pipefail
n=$1
cd "$(dirname "$0")/../.."
export PATH=$HOME/bonetto-venv/bin:/opt/hwe-toolchain/oss-cad-suite/bin:$PATH
export BONETTO_HWE_HEAVY_SLOTS=9
mkdir -p build
rm -f build/sw-$n.rc
setsid nohup bash -c "tools/harden_tt.sh --overlay signoff/overlays/$n.json --timeout 21600 --out build/harden-$n > build/sw-$n.log 2>&1; echo \$? > build/sw-$n.rc" < /dev/null > /dev/null 2>&1 &
echo $! > build/sw-$n.pid
echo "$n pid $(cat build/sw-$n.pid)"
