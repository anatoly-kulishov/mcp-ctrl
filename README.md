# mcp-ctrl

Универсальный пульт для MCP-серверов и скиллов across **OpenCode**, **Cursor** и **Codex**.

Один CLI: status, plan, profile, токены, doctor, symlink-first skills. Stdlib-only Python 3.11+.

Почему не [vsync](https://github.com/nicepkg/vsync): он не знает XDG-путь OpenCode (`~/.config/opencode`), не читает per-skill symlinks из `~/.agents/skills`, и не закрывает схему секретов (`{file:}` + Cursor inline без `$VAR`). Идеи взяты: `plan`, safe/prune, manifest/hash, profiles.

## Install

```bash
git clone https://github.com/anatoly-kulishov/mcp-ctrl.git
cd mcp-ctrl
./install.sh
# ensure ~/.local/bin is on PATH
mcp-ctrl status
```

## Typical flow

```bash
mcp-ctrl plan
mcp-ctrl profile list
mcp-ctrl profile apply default
mcp-ctrl token set youtrack ~/.config/opencode/secrets/youtrack-token
mcp-ctrl skills sync          # safe: add/relink only
mcp-ctrl skills sync --prune  # also remove extras in targets
mcp-ctrl doctor
```

Секреты **не** в репозитории. Каталог описывает только имена env-ключей.

## Commands

| Command | Purpose |
|---------|---------|
| `status` | server × harness matrix |
| `plan` | MCP + skills sync plan (no writes) |
| `list [harness]` | details (secrets masked) |
| `mcp add\|remove\|enable\|disable` | mutate configs |
| `token set\|source` | update / inspect tokens |
| `skills list\|sync [--prune] [--dry-run]` | symlink-first from `~/.agents/skills` |
| `profile list\|apply` | named MCP+skills sets |
| `doctor [--fix]` | consistency + manifest drift |
| `catalog list` | shared MCP recipes |

## Profiles

- `profiles/default.jsonc` — playwright + youtrack, full KEEP skills
- `profiles/frontend.jsonc` — UI subset

Canonical skills list: `skills/manifest.txt` (SoT). `~/.agents/sync-skills.sh` остаётся совместимым fallback.

## Safety

- Secrets masked in output
- Secret-bearing files get `chmod 600`
- Backup before first edit: `<file>.bak-<timestamp>`
- Skills: **safe** by default; `--prune` for strict mirror
- Manifest: `~/.config/mcp-ctrl/manifest.json` (hashes, no secrets)
- Cursor does **not** expand `$VAR` — use `token set` for real inline values
