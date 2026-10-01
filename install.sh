#!/usr/bin/env bash
set -euo pipefail
task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
task_dest="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/st87.side-display"
task_data="${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-side-display"
task_shell="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/shell.json"
command -v uv >/dev/null || { echo "Install uv first: omarchy pkg add uv" >&2; exit 1; }
command -v wayvnc >/dev/null || omarchy pkg add wayvnc
omarchy plugin validate "$task_root"
UV_PROJECT_ENVIRONMENT="$task_data/venv" uv sync --project "$task_root" --python 3.13 --locked --no-dev
if [[ "$task_root" != "$task_dest" ]]; then
  if [[ -d "$task_dest" ]]; then
    bash "$task_dest/side-display" stop
    mkdir -p "$task_data/backups"
    cp -a -- "$task_dest" "$task_data/backups/plugin.$(date +%s)"
  fi
  mkdir -p "$task_dest"
  for task_file in manifest.json Panel.qml side-display hotspot-helper.py install.sh uninstall.sh pyproject.toml uv.lock README.md LICENSE; do
    cp -- "$task_root/$task_file" "$task_dest/$task_file"
  done
  cp -r -- "$task_root/side_display" "$task_root/web" "$task_dest/"
fi
if [[ -f "$task_shell" ]]; then
  cp -a -- "$task_shell" "$task_data/shell.json.bak.$(date +%s)"
fi
omarchy-shell shell rescanPlugins
omarchy plugin enable st87.side-display --section right --before omarchy.monitor
printf 'Installed Tesla Side Display. Click its monitor icon in the bar to start sharing.\n'
