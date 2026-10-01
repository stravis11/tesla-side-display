#!/usr/bin/env bash
set -euo pipefail
task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bash "$task_root/side-display" stop
omarchy plugin disable st87.side-display
printf 'Sharing stopped and the plugin disabled. Source and Python environment are preserved.\n'
