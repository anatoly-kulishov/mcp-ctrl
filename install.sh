#!/usr/bin/env bash
# Install mcp-ctrl into ~/.local/bin and seed ~/.config/mcp-ctrl
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/mcp-ctrl"
TARGET="${BIN_DIR}/mcp-ctrl"

mkdir -p "$BIN_DIR" "$CFG_DIR"

chmod +x "${ROOT}/mcp-ctrl.py"
ln -sfn "${ROOT}/mcp-ctrl.py" "$TARGET"

if [[ ! -f "${CFG_DIR}/config.jsonc" ]]; then
  cat > "${CFG_DIR}/config.jsonc" <<'EOF'
{
  // Optional overrides. Empty / omitted keys use $HOME + XDG autodetection.
  "paths": {
    // "opencode_config": "~/.config/opencode/opencode.jsonc",
    // "cursor_config": "~/.cursor/mcp.json",
    // "codex_config": "~/.codex/config.toml",
    // "opencode_secrets": "~/.config/opencode/secrets",
    // "sync_skills": "~/.agents/sync-skills.sh",
    // "catalog_dir": ""  // defaults to <repo>/catalog
  },
  "doctor_ignore_servers": ["node_repl"],
  "doctor_optional_servers": ["figma"]
}
EOF
  echo "Wrote ${CFG_DIR}/config.jsonc"
fi

echo "Installed: ${TARGET} -> ${ROOT}/mcp-ctrl.py"

case ":$PATH:" in
  *":${BIN_DIR}:"*) echo "OK: ${BIN_DIR} is on PATH" ;;
  *)
    echo "NOTE: ${BIN_DIR} is not on PATH. Add to ~/.zshrc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

echo "Try: mcp-ctrl status"
