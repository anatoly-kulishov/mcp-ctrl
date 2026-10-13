#!/usr/bin/env python3
"""mcp-ctrl — universal remote for MCP + skills across OpenCode / Cursor / Codex.

Stdlib only. Portable ($HOME / XDG). Secrets masked; secret files chmod 600.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

VERSION = "0.2.0"
HARNESS_NAMES = ("opencode", "cursor", "codex")
SECRET_MARKERS = ("TOKEN", "API_KEY", "SECRET", "PASSWORD", "AUTH", "KEY")


# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------

def home() -> Path:
    return Path.home()


def xdg_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", home() / ".config"))


def tool_root() -> Path:
    """Directory containing this script (repo root when installed via symlink)."""
    return Path(__file__).resolve().parent


def tool_config_dir() -> Path:
    return xdg_config() / "mcp-ctrl"


def expand_user_path(p: str | Path) -> Path:
    return Path(os.path.expanduser(str(p))).expanduser().resolve()


@dataclass
class Paths:
    opencode_config: Path
    cursor_config: Path
    codex_config: Path
    opencode_secrets: Path
    sync_skills: Path
    agents_skills: Path
    cursor_skills: Path
    opencode_skills: Path
    codex_skills: Path
    catalog_dir: Path
    skills_manifest: Path
    profiles_dir: Path

    @classmethod
    def autodect(cls) -> "Paths":
        cfg = xdg_config()
        h = home()
        root = tool_root()
        catalog = root / "catalog"
        if not catalog.is_dir():
            catalog = tool_config_dir() / "catalog"
        profiles = root / "profiles"
        if not profiles.is_dir():
            profiles = tool_config_dir() / "profiles"
        manifest = root / "skills" / "manifest.txt"
        if not manifest.is_file():
            manifest = tool_config_dir() / "skills" / "manifest.txt"
        return cls(
            opencode_config=cfg / "opencode" / "opencode.jsonc",
            cursor_config=h / ".cursor" / "mcp.json",
            codex_config=h / ".codex" / "config.toml",
            opencode_secrets=cfg / "opencode" / "secrets",
            sync_skills=h / ".agents" / "sync-skills.sh",
            agents_skills=h / ".agents" / "skills",
            cursor_skills=h / ".cursor" / "skills",
            opencode_skills=cfg / "opencode" / "skills",
            codex_skills=h / ".codex" / "skills",
            catalog_dir=catalog,
            skills_manifest=manifest,
            profiles_dir=profiles,
        )

    @classmethod
    def load(cls) -> "Paths":
        base = cls.autodect()
        cfg_path = tool_config_dir() / "config.jsonc"
        if not cfg_path.is_file():
            return base
        try:
            data = load_jsonc(cfg_path.read_text(encoding="utf-8"))
        except Exception as e:
            die(f"invalid {cfg_path}: {e}")
        paths = data.get("paths") or {}

        def pick(key: str, default: Path) -> Path:
            v = paths.get(key)
            return expand_user_path(v) if v else default

        return cls(
            opencode_config=pick("opencode_config", base.opencode_config),
            cursor_config=pick("cursor_config", base.cursor_config),
            codex_config=pick("codex_config", base.codex_config),
            opencode_secrets=pick("opencode_secrets", base.opencode_secrets),
            sync_skills=pick("sync_skills", base.sync_skills),
            agents_skills=pick("agents_skills", base.agents_skills),
            cursor_skills=pick("cursor_skills", base.cursor_skills),
            opencode_skills=pick("opencode_skills", base.opencode_skills),
            codex_skills=pick("codex_skills", base.codex_skills),
            catalog_dir=pick("catalog_dir", base.catalog_dir),
            skills_manifest=pick("skills_manifest", base.skills_manifest),
            profiles_dir=pick("profiles_dir", base.profiles_dir),
        )

    def skill_dests(self) -> dict[str, Path]:
        return {
            "cursor": self.cursor_skills,
            "opencode": self.opencode_skills,
            "codex": self.codex_skills,
        }


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def mask_secret(value: str | None) -> str:
    if value is None or value == "":
        return "(empty)"
    if value.startswith("{file:") or value.startswith("${") or value.startswith("$"):
        return value
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-2:]


def is_secret_key(key: str) -> bool:
    u = key.upper()
    if u.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY")):
        return True
    return any(m in u for m in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))


def mask_env(env: dict[str, str]) -> dict[str, str]:
    return {k: (mask_secret(v) if is_secret_key(k) else v) for k, v in env.items()}


def chmod600(path: Path) -> None:
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def backup_file(path: Path, dry_run: bool) -> Path | None:
    if not path.is_file():
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak-{ts}")
    if dry_run:
        print(f"  dry-run: backup {path} -> {bak}")
        return bak
    shutil.copy2(path, bak)
    chmod600(bak)
    return bak


def write_text(path: Path, text: str, *, dry_run: bool, secret: bool = False) -> None:
    if dry_run:
        print(f"  dry-run: write {path} ({len(text)} bytes)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if secret:
        chmod600(path)


def token_hash(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def default_token_key(server: str) -> str:
    return f"{server.upper().replace('-', '_')}_TOKEN"


def parse_env_pairs(pairs: Sequence[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in pairs or []:
        if "=" not in p:
            die(f"invalid --env {p!r}, expected KEY=value")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def parse_harness_list(raw: str | None) -> list[str]:
    if not raw or raw.strip() in ("", "all"):
        return list(HARNESS_NAMES)
    names = [x.strip().lower() for x in raw.split(",") if x.strip()]
    bad = [n for n in names if n not in HARNESS_NAMES]
    if bad:
        die(f"unknown harness: {', '.join(bad)} (want: {', '.join(HARNESS_NAMES)})")
    return names


# ---------------------------------------------------------------------------
# JSONC
# ---------------------------------------------------------------------------

def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments outside of strings."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_str = False
    esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                i += 2
                while i < n and text[i] not in "\r\n":
                    i += 1
                continue
            if nxt == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i = min(i + 2, n)
                continue
        out.append(c)
        i += 1
    return "".join(out)


def load_jsonc(text: str) -> Any:
    return json.loads(strip_jsonc(text))


def _skip_ws(text: str, i: int) -> int:
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    return i


def _skip_string(text: str, i: int) -> int:
    """i points at opening quote; return index after closing quote."""
    assert text[i] == '"'
    i += 1
    n = len(text)
    esc = False
    while i < n:
        c = text[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            return i + 1
        i += 1
    raise ValueError("unterminated string")


def _skip_value(text: str, i: int) -> int:
    i = _skip_ws(text, i)
    if i >= len(text):
        raise ValueError("unexpected EOF")
    c = text[i]
    if c == '"':
        return _skip_string(text, i)
    if c in "{[":
        return _match_brackets(text, i)[1]
    if c in "-0123456789":
        while i < len(text) and text[i] not in ",}] \t\r\n":
            i += 1
        return i
    for lit in ("true", "false", "null"):
        if text.startswith(lit, i):
            return i + len(lit)
    raise ValueError(f"cannot skip value at {i}")


def _match_brackets(text: str, start: int) -> tuple[int, int]:
    """Return (start, end_exclusive) of {...} or [...] starting at start."""
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    i = start + 1
    n = len(text)
    depth = 1
    while i < n:
        c = text[i]
        if c == '"':
            i = _skip_string(text, i)
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return start, i + 1
        i += 1
    raise ValueError("unbalanced brackets")


def _find_key_in_object(text: str, obj_start: int, key: str) -> tuple[int, int, int, int] | None:
    """Inside object starting at obj_start, find key.

    Returns (key_quote_start, colon_pos, value_start, value_end) or None.
    key_quote_start is the opening " of the key (for deletion of whole entry).
    """
    _, obj_end = _match_brackets(text, obj_start)
    i = obj_start + 1
    target = f'"{key}"'
    while i < obj_end - 1:
        i = _skip_ws(text, i)
        if i >= obj_end - 1:
            break
        if text[i] == ",":
            i += 1
            continue
        if text[i] != '"':
            # skip unexpected (comments already stripped for search on raw — scan carefully)
            # In raw JSONC comments may exist; skip // and /* */ here too
            if text.startswith("//", i):
                while i < obj_end and text[i] not in "\r\n":
                    i += 1
                continue
            if text.startswith("/*", i):
                j = text.find("*/", i + 2)
                i = j + 2 if j != -1 else obj_end
                continue
            i += 1
            continue
        key_start = i
        key_end = _skip_string(text, i)
        key_text = text[key_start:key_end]
        i = _skip_ws(text, key_end)
        if i >= obj_end or text[i] != ":":
            continue
        colon = i
        i = _skip_ws(text, i + 1)
        val_start = i
        val_end = _skip_value(text, i)
        if key_text == target:
            return key_start, colon, val_start, val_end
        i = val_end
    return None


def _find_nested_object(text: str, keys: Sequence[str]) -> int | None:
    """Return start index of nested object for path keys (top-level object)."""
    text_stripped_start = _skip_ws(text, 0)
    if text_stripped_start >= len(text) or text[text_stripped_start] != "{":
        # allow leading comments
        i = 0
        while i < len(text):
            i = _skip_ws(text, i)
            if text.startswith("//", i):
                while i < len(text) and text[i] not in "\r\n":
                    i += 1
                continue
            if text.startswith("/*", i):
                j = text.find("*/", i + 2)
                i = j + 2 if j != -1 else len(text)
                continue
            break
        if i >= len(text) or text[i] != "{":
            return None
        root = i
    else:
        root = text_stripped_start

    obj = root
    for k in keys:
        found = _find_key_in_object(text, obj, k)
        if not found:
            return None
        _, _, vs, ve = found
        vs = _skip_ws(text, vs)
        if vs >= len(text) or text[vs] != "{":
            return None
        obj = vs
    return obj


def jsonc_get_object(text: str, keys: Sequence[str]) -> dict[str, Any]:
    data = load_jsonc(text)
    cur: Any = data
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return {}
        cur = cur[k]
    return cur if isinstance(cur, dict) else {}


def jsonc_set_key(
    text: str,
    parent_keys: Sequence[str],
    key: str,
    value_json: str,
    *,
    create_parents: bool = True,
) -> str:
    """Set key under nested object path to value_json (already serialized JSON text)."""
    obj_start = _find_nested_object(text, parent_keys)
    if obj_start is None:
        if not create_parents:
            raise KeyError(f"missing path {'.'.join(parent_keys)}")
        # create path from root
        text = _jsonc_ensure_path(text, parent_keys)
        obj_start = _find_nested_object(text, parent_keys)
        if obj_start is None:
            raise RuntimeError("failed to create parent path")

    found = _find_key_in_object(text, obj_start, key)
    if found:
        _, _, vs, ve = found
        return text[:vs] + value_json + text[ve:]

    # insert before closing brace of object
    _, obj_end = _match_brackets(text, obj_start)
    insert_at = obj_end - 1
    # find last non-ws before }
    j = insert_at - 1
    while j > obj_start and text[j] in " \t\r\n":
        j -= 1
    need_comma = j > obj_start and text[j] not in "{,"
    # indent: detect from existing keys or default 2 spaces * depth
    indent = _detect_indent(text, obj_start)
    entry = f'{indent}"{key}": {value_json}'
    prefix = "," if need_comma else ""
    block = f"{prefix}\n{entry}\n"
    # if object empty, nicer formatting
    inner = text[obj_start + 1 : insert_at].strip()
    if not inner:
        block = f"\n{entry}\n"
    return text[:insert_at] + block + text[insert_at:]


def _detect_indent(text: str, obj_start: int) -> str:
    # look for next quoted key newline indent
    m = re.search(r'\n([ \t]+)"', text[obj_start : obj_start + 500])
    if m:
        return m.group(1)
    return "  "


def _jsonc_ensure_path(text: str, keys: Sequence[str]) -> str:
    if not keys:
        return text
    # ensure top-level { }
    i = 0
    while i < len(text):
        i = _skip_ws(text, i)
        if text.startswith("//", i):
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        break
    if i >= len(text) or text[i] != "{":
        text = "{\n}\n"
    built: list[str] = []
    for k in keys:
        path = built + [k]
        if _find_nested_object(text, path) is None:
            parent = built
            empty = "{\n  }"
            if parent:
                text = jsonc_set_key(text, parent, k, empty, create_parents=False)
            else:
                text = jsonc_set_key(text, [], k, empty, create_parents=False)
        built.append(k)
    return text


def jsonc_delete_key(text: str, parent_keys: Sequence[str], key: str) -> str:
    obj_start = _find_nested_object(text, parent_keys)
    if obj_start is None:
        return text
    found = _find_key_in_object(text, obj_start, key)
    if not found:
        return text
    key_start, _, _, val_end = found
    # include trailing comma / preceding comma
    start = key_start
    end = val_end
    # eat trailing whitespace + comma
    j = end
    while j < len(text) and text[j] in " \t":
        j += 1
    if j < len(text) and text[j] == ",":
        end = j + 1
        while end < len(text) and text[end] in " \t":
            end += 1
        if end < len(text) and text[end] in "\r\n":
            end += 1
            if end < len(text) and text[end - 1] == "\r" and text[end] == "\n":
                end += 1
    else:
        # maybe preceding comma
        k = start - 1
        while k >= 0 and text[k] in " \t":
            k -= 1
        if k >= 0 and text[k] == ",":
            start = k
        # eat leading newline before key
        while start > 0 and text[start - 1] in " \t":
            start -= 1
        if start > 0 and text[start - 1] in "\r\n":
            start -= 1
            if start > 0 and text[start - 1] == "\r":
                start -= 1
    return text[:start] + text[end:]


def jsonc_set_bool_in_object(text: str, parent_keys: Sequence[str], key: str, obj_key: str, value: bool) -> str:
    """Set boolean field inside object at parent_keys+key."""
    path = list(parent_keys) + [key]
    obj = _find_nested_object(text, path)
    if obj is None:
        raise KeyError(key)
    lit = "true" if value else "false"
    found = _find_key_in_object(text, obj, obj_key)
    if found:
        _, _, vs, ve = found
        return text[:vs] + lit + text[ve:]
    return jsonc_set_key(text, path, obj_key, lit)


def dumps_json_compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# TOML (text editor)
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*(?:#.*)?$", re.M)


def toml_load(path: Path) -> dict[str, Any]:
    if tomllib is None:
        die("tomllib required (Python 3.11+)")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _toml_section_spans(text: str) -> list[tuple[str, int, int]]:
    """List of (section_name, start, end_exclusive) covering each section body+header."""
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return []
    spans: list[tuple[str, int, int]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        spans.append((m.group(1), start, end))
    return spans


def toml_find_sections(text: str, prefix: str) -> list[tuple[str, int, int]]:
    """Sections whose name == prefix or name.startswith(prefix+'.')."""
    out = []
    for name, start, end in _toml_section_spans(text):
        if name == prefix or name.startswith(prefix + "."):
            out.append((name, start, end))
    return out


def toml_remove_sections(text: str, prefix: str) -> str:
    secs = toml_find_sections(text, prefix)
    if not secs:
        return text
    # remove from first to last contiguous? may be non-contiguous — remove each reverse
    for name, start, end in sorted(secs, key=lambda x: x[1], reverse=True):
        text = text[:start] + text[end:]
    # tidy triple newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def toml_upsert_server_block(text: str, name: str, block: str, *, disabled: bool = False) -> str:
    root = f"mcp_servers_disabled.{name}" if disabled else f"mcp_servers.{name}"
    text = toml_remove_sections(text, root)
    if not text.endswith("\n"):
        text += "\n"
    if not text.endswith("\n\n"):
        text += "\n"
    return text + block.rstrip() + "\n"


def toml_escape(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def toml_format_server(
    name: str,
    *,
    command: str | None = None,
    args: list[str] | None = None,
    url: str | None = None,
    env: dict[str, str] | None = None,
    disabled: bool = False,
    extra_lines: list[str] | None = None,
) -> str:
    root = f"mcp_servers_disabled.{name}" if disabled else f"mcp_servers.{name}"
    lines = [f"[{root}]"]
    if command is not None:
        lines.append(f"command = {toml_escape(command)}")
    if args:
        args_s = ", ".join(toml_escape(a) for a in args)
        lines.append(f"args = [{args_s}]")
    if url is not None:
        lines.append(f"url = {toml_escape(url)}")
    for el in extra_lines or []:
        lines.append(el)
    if env:
        lines.append("")
        lines.append(f"[{root}.env]")
        for k, v in env.items():
            lines.append(f"{k} = {toml_escape(v)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Canonical model
# ---------------------------------------------------------------------------

@dataclass
class ServerSpec:
    name: str
    type: str = "stdio"  # stdio | remote
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    enabled: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    def token_keys(self) -> list[str]:
        return [k for k in self.env if is_secret_key(k)]


@dataclass
class ServerState:
    harness: str
    name: str
    present: bool
    enabled: bool | None  # None = N/A / unknown
    spec: ServerSpec | None = None
    token_source: str | None = None  # inline | file | env | missing
    token_preview: str | None = None
    token_digest: str | None = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Harness adapters
# ---------------------------------------------------------------------------

class Harness:
    name: str

    def __init__(self, paths: Paths, dry_run: bool = False):
        self.paths = paths
        self.dry_run = dry_run

    @property
    def config_path(self) -> Path:
        raise NotImplementedError

    def available(self) -> bool:
        return self.config_path.is_file()

    def list_servers(self) -> list[ServerSpec]:
        raise NotImplementedError

    def get(self, name: str) -> ServerSpec | None:
        for s in self.list_servers():
            if s.name == name:
                return s
        return None

    def add(self, spec: ServerSpec) -> None:
        raise NotImplementedError

    def remove(self, name: str) -> None:
        raise NotImplementedError

    def set_enabled(self, name: str, enabled: bool) -> None:
        raise NotImplementedError

    def token_info(self, name: str) -> tuple[str, str | None]:
        """Return (source, resolved_value_or_None)."""
        raise NotImplementedError

    def token_set(self, name: str, value: str, env_key: str | None = None) -> None:
        raise NotImplementedError

    def _write(self, path: Path, new_text: str, old_text: str, *, secret: bool = False) -> None:
        if new_text == old_text:
            print(f"  {self.name}: unchanged")
            return
        backup_file(path, self.dry_run)
        write_text(path, new_text, dry_run=self.dry_run, secret=secret)
        print(f"  {self.name}: updated {path}")


class OpenCodeHarness(Harness):
    name = "opencode"

    @property
    def config_path(self) -> Path:
        return self.paths.opencode_config

    def list_servers(self) -> list[ServerSpec]:
        if not self.available():
            return []
        data = load_jsonc(self.config_path.read_text(encoding="utf-8"))
        mcp = data.get("mcp") or {}
        out: list[ServerSpec] = []
        for name, cfg in mcp.items():
            if not isinstance(cfg, dict):
                continue
            t = cfg.get("type", "local")
            stype = "remote" if t == "remote" else "stdio"
            cmd = cfg.get("command")
            command = None
            args: list[str] = []
            if isinstance(cmd, list) and cmd:
                command = str(cmd[0])
                args = [str(x) for x in cmd[1:]]
            elif isinstance(cmd, str):
                command = cmd
            env = {str(k): str(v) for k, v in (cfg.get("environment") or {}).items()}
            out.append(
                ServerSpec(
                    name=name,
                    type=stype,
                    command=command,
                    args=args,
                    env=env,
                    url=cfg.get("url"),
                    enabled=bool(cfg.get("enabled", True)),
                    raw=cfg,
                )
            )
        return out

    def add(self, spec: ServerSpec) -> None:
        path = self.config_path
        text = path.read_text(encoding="utf-8") if path.is_file() else "{\n}\n"
        if spec.type == "remote":
            obj: dict[str, Any] = {
                "type": "remote",
                "url": spec.url or "",
                "enabled": spec.enabled,
            }
        else:
            cmd = [spec.command or ""] + list(spec.args)
            env = dict(spec.env)
            # prefer {file:} for secrets if not already
            for k, v in list(env.items()):
                if is_secret_key(k) and not str(v).startswith("{file:"):
                    secret_path = self.paths.opencode_secrets / f"{spec.name}-token"
                    env[k] = f"{{file:{secret_path}}}"
                    if v and v not in ("", "PLACEHOLDER"):
                        write_text(secret_path, v.rstrip("\n") + "\n", dry_run=self.dry_run, secret=True)
            obj = {
                "type": "local",
                "command": cmd,
                "environment": env,
                "enabled": spec.enabled,
            }
            if not env:
                obj.pop("environment", None)
        value = dumps_json_compact(obj)
        # indent nested for readability — dumps already indented; re-indent relative
        value = _reindent_block(value, "    ")
        new = jsonc_set_key(text, ["mcp"], spec.name, value)
        self._write(path, new, text)

    def remove(self, name: str) -> None:
        path = self.config_path
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8")
        new = jsonc_delete_key(text, ["mcp"], name)
        self._write(path, new, text)

    def set_enabled(self, name: str, enabled: bool) -> None:
        path = self.config_path
        text = path.read_text(encoding="utf-8")
        try:
            new = jsonc_set_bool_in_object(text, ["mcp"], name, "enabled", enabled)
        except KeyError:
            die(f"opencode: server {name!r} not found")
        self._write(path, new, text)

    def _resolve_file_ref(self, ref: str) -> str | None:
        m = re.match(r"^\{file:(.+)\}$", ref.strip())
        if not m:
            return None
        p = expand_user_path(m.group(1).strip())
        if not p.is_file():
            return None
        return p.read_text(encoding="utf-8").strip()

    def token_info(self, name: str) -> tuple[str, str | None]:
        spec = self.get(name)
        if not spec:
            return "missing", None
        keys = spec.token_keys() or [default_token_key(name)]
        for k in keys:
            v = spec.env.get(k)
            if v is None:
                continue
            if str(v).startswith("{file:"):
                resolved = self._resolve_file_ref(str(v))
                return "file", resolved
            if str(v).startswith("${") or str(v).startswith("$"):
                return "env", os.environ.get(_env_var_name(str(v)))
            return "inline", str(v)
        return "missing", None

    def token_set(self, name: str, value: str, env_key: str | None = None) -> None:
        spec = self.get(name)
        key = env_key or (spec.token_keys()[0] if spec and spec.token_keys() else default_token_key(name))
        secret_path = self.paths.opencode_secrets / f"{name}-token"
        write_text(secret_path, value.rstrip("\n") + "\n", dry_run=self.dry_run, secret=True)
        print(f"  opencode: secret file {secret_path}")
        # ensure config points to {file:}
        path = self.config_path
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8")
        data = load_jsonc(text)
        mcp = (data.get("mcp") or {}).get(name)
        if not isinstance(mcp, dict):
            warn(f"opencode: {name} not in config; secret written but config untouched")
            return
        env = dict(mcp.get("environment") or {})
        want_tilde = f"{{file:{_tilde_path(secret_path)}}}"
        cur = env.get(key, "")
        if isinstance(cur, str) and cur.startswith("{file:"):
            # keep existing {file:} path; value already written to secret_path above
            # if file ref points elsewhere, also write there for consistency
            m = re.match(r"^\{file:(.+)\}$", cur.strip())
            if m:
                alt = expand_user_path(m.group(1).strip())
                if alt != secret_path.resolve() and alt != expand_user_path(secret_path):
                    write_text(alt, value.rstrip("\n") + "\n", dry_run=self.dry_run, secret=True)
                    print(f"  opencode: also wrote {alt}")
            print(f"  opencode: config already uses {cur}")
            return
        server_path = ["mcp", name]
        obj_start = _find_nested_object(text, server_path)
        if obj_start is None:
            die(f"opencode: cannot find mcp.{name}")
        env_found = _find_key_in_object(text, obj_start, "environment")
        if env_found:
            _, _, vs, _ = env_found
            vs = _skip_ws(text, vs)
            if text[vs] == "{":
                inner = _find_key_in_object(text, vs, key)
                if inner:
                    _, _, ivs, ive = inner
                    new = text[:ivs] + json.dumps(want_tilde) + text[ive:]
                else:
                    new = jsonc_set_key(text, server_path + ["environment"], key, json.dumps(want_tilde))
            else:
                new = jsonc_set_key(text, server_path, "environment", dumps_json_compact({key: want_tilde}))
        else:
            new = jsonc_set_key(text, server_path, "environment", dumps_json_compact({key: want_tilde}))
        self._write(path, new, text)


class CursorHarness(Harness):
    """Cursor mcp.json is plain JSON (no comments) — rewrite whole file safely."""

    name = "cursor"
    ACTIVE = "mcpServers"
    DISABLED = "_disabledMcpServers"

    @property
    def config_path(self) -> Path:
        return self.paths.cursor_config

    def _load(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            return {self.ACTIVE: {}}
        data = load_jsonc(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {self.ACTIVE: {}}
        data.setdefault(self.ACTIVE, {})
        return data

    def _dump(self, data: dict[str, Any]) -> None:
        # drop empty disabled bucket
        if self.DISABLED in data and not data[self.DISABLED]:
            data.pop(self.DISABLED, None)
        old = self.config_path.read_text(encoding="utf-8") if self.config_path.is_file() else ""
        new = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        self._write(self.config_path, new, old, secret=True)

    def _spec_to_cfg(self, spec: ServerSpec) -> dict[str, Any]:
        if spec.type == "remote":
            return {"url": spec.url or ""}
        obj: dict[str, Any] = {"command": spec.command or ""}
        if spec.args:
            obj["args"] = spec.args
        if spec.env:
            obj["env"] = spec.env
        return obj

    def list_servers(self) -> list[ServerSpec]:
        if not self.available():
            return []
        data = self._load()
        out: list[ServerSpec] = []
        for bucket, enabled in ((self.ACTIVE, True), (self.DISABLED, False)):
            for name, cfg in (data.get(bucket) or {}).items():
                if not isinstance(cfg, dict):
                    continue
                out.append(self._from_cfg(name, cfg, enabled))
        return out

    def _from_cfg(self, name: str, cfg: dict, enabled: bool) -> ServerSpec:
        url = cfg.get("url")
        stype = "remote" if url else "stdio"
        return ServerSpec(
            name=name,
            type=stype,
            command=cfg.get("command"),
            args=[str(x) for x in (cfg.get("args") or [])],
            env={str(k): str(v) for k, v in (cfg.get("env") or {}).items()},
            url=url,
            enabled=enabled,
            raw=cfg,
        )

    def add(self, spec: ServerSpec) -> None:
        data = self._load()
        data.setdefault(self.ACTIVE, {})
        data.setdefault(self.DISABLED, {})
        data[self.ACTIVE].pop(spec.name, None)
        data[self.DISABLED].pop(spec.name, None)
        bucket = self.ACTIVE if spec.enabled else self.DISABLED
        data[bucket][spec.name] = self._spec_to_cfg(spec)
        self._dump(data)

    def remove(self, name: str) -> None:
        data = self._load()
        changed = False
        for bucket in (self.ACTIVE, self.DISABLED):
            if name in (data.get(bucket) or {}):
                del data[bucket][name]
                changed = True
        if changed:
            self._dump(data)
        else:
            print(f"  cursor: {name} not found")

    def set_enabled(self, name: str, enabled: bool) -> None:
        data = self._load()
        cfg = None
        src = None
        for bucket in (self.ACTIVE, self.DISABLED):
            if name in (data.get(bucket) or {}):
                cfg = data[bucket][name]
                src = bucket
                break
        if cfg is None:
            die(f"cursor: server {name!r} not found")
        dest = self.ACTIVE if enabled else self.DISABLED
        if src == dest:
            print(f"  cursor: already {'enabled' if enabled else 'disabled'}")
            return
        data.setdefault(dest, {})
        data[dest][name] = cfg
        del data[src][name]
        self._dump(data)

    def token_info(self, name: str) -> tuple[str, str | None]:
        spec = self.get(name)
        if not spec:
            return "missing", None
        keys = spec.token_keys() or [default_token_key(name)]
        for k in keys:
            v = spec.env.get(k)
            if v is None:
                continue
            if str(v).startswith("${") or (str(v).startswith("$") and not str(v).startswith("$HOME")):
                var = _env_var_name(str(v))
                return "env", os.environ.get(var)
            return "inline", str(v)
        return "missing", None

    def token_set(self, name: str, value: str, env_key: str | None = None) -> None:
        data = self._load()
        bucket = None
        for b in (self.ACTIVE, self.DISABLED):
            if name in (data.get(b) or {}):
                bucket = b
                break
        if not bucket:
            die(f"cursor: server {name!r} not found")
        cfg = data[bucket][name]
        if not isinstance(cfg, dict):
            die(f"cursor: invalid cfg for {name}")
        env = dict(cfg.get("env") or {})
        key = env_key or next((k for k in env if is_secret_key(k)), default_token_key(name))
        if env.get(key) == value:
            print("  cursor: unchanged")
            return
        env[key] = value
        cfg["env"] = env
        data[bucket][name] = cfg
        self._dump(data)


class CodexHarness(Harness):
    name = "codex"

    @property
    def config_path(self) -> Path:
        return self.paths.codex_config

    def list_servers(self) -> list[ServerSpec]:
        if not self.available():
            return []
        data = toml_load(self.config_path)
        out: list[ServerSpec] = []
        for bucket, enabled in (("mcp_servers", True), ("mcp_servers_disabled", False)):
            servers = data.get(bucket) or {}
            if not isinstance(servers, dict):
                continue
            for name, cfg in servers.items():
                if not isinstance(cfg, dict):
                    continue
                env = cfg.get("env") or {}
                if not isinstance(env, dict):
                    env = {}
                url = cfg.get("url")
                out.append(
                    ServerSpec(
                        name=name,
                        type="remote" if url else "stdio",
                        command=cfg.get("command"),
                        args=[str(x) for x in (cfg.get("args") or [])],
                        env={str(k): str(v) for k, v in env.items()},
                        url=url,
                        enabled=enabled,
                        raw=cfg,
                    )
                )
        return out

    def _extra_keys(self, cfg: dict) -> list[str]:
        skip = {"command", "args", "url", "env"}
        lines = []
        for k, v in cfg.items():
            if k in skip:
                continue
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                lines.append(f"{k} = {v}")
            elif isinstance(v, str):
                lines.append(f"{k} = {toml_escape(v)}")
            elif isinstance(v, list) and all(isinstance(x, str) for x in v):
                args_s = ", ".join(toml_escape(a) for a in v)
                lines.append(f"{k} = [{args_s}]")
        return lines

    def add(self, spec: ServerSpec) -> None:
        path = self.config_path
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        text = toml_remove_sections(old, f"mcp_servers.{spec.name}")
        text = toml_remove_sections(text, f"mcp_servers_disabled.{spec.name}")
        block = toml_format_server(
            spec.name,
            command=None if spec.type == "remote" else (spec.command or ""),
            args=spec.args if spec.type != "remote" else None,
            url=spec.url if spec.type == "remote" else None,
            env=spec.env or None,
            disabled=not spec.enabled,
        )
        new = toml_upsert_server_block(text, spec.name, block, disabled=not spec.enabled)
        self._write(path, new, old, secret=True)

    def remove(self, name: str) -> None:
        path = self.config_path
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8")
        new = toml_remove_sections(text, f"mcp_servers.{name}")
        new = toml_remove_sections(new, f"mcp_servers_disabled.{name}")
        self._write(path, new, text, secret=True)

    def set_enabled(self, name: str, enabled: bool) -> None:
        path = self.config_path
        text = path.read_text(encoding="utf-8")
        data = toml_load(path)
        cfg = None
        src_disabled = False
        for bucket, dis in (("mcp_servers", False), ("mcp_servers_disabled", True)):
            servers = data.get(bucket) or {}
            if name in servers and isinstance(servers[name], dict):
                cfg = servers[name]
                src_disabled = dis
                break
        if cfg is None:
            die(f"codex: server {name!r} not found")
        want_disabled = not enabled
        if src_disabled == want_disabled:
            print(f"  codex: already {'enabled' if enabled else 'disabled'}")
            return
        env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
        block = toml_format_server(
            name,
            command=cfg.get("command"),
            args=list(cfg.get("args") or []) or None,
            url=cfg.get("url"),
            env={str(k): str(v) for k, v in env.items()} or None,
            disabled=want_disabled,
            extra_lines=self._extra_keys(cfg),
        )
        new = toml_remove_sections(text, f"mcp_servers.{name}")
        new = toml_remove_sections(new, f"mcp_servers_disabled.{name}")
        new = toml_upsert_server_block(new, name, block, disabled=want_disabled)
        self._write(path, new, text, secret=True)

    def token_info(self, name: str) -> tuple[str, str | None]:
        spec = self.get(name)
        if not spec:
            return "missing", None
        keys = spec.token_keys() or [default_token_key(name)]
        for k in keys:
            v = spec.env.get(k)
            if v is None:
                continue
            return "inline", str(v)
        return "missing", None

    def token_set(self, name: str, value: str, env_key: str | None = None) -> None:
        path = self.config_path
        text = path.read_text(encoding="utf-8")
        data = toml_load(path)
        disabled = False
        cfg = None
        for bucket, dis in (("mcp_servers", False), ("mcp_servers_disabled", True)):
            servers = data.get(bucket) or {}
            if name in servers:
                cfg = servers[name]
                disabled = dis
                break
        if cfg is None or not isinstance(cfg, dict):
            die(f"codex: server {name!r} not found")
        env = dict(cfg.get("env") or {})
        key = env_key or next((k for k in env if is_secret_key(k)), default_token_key(name))
        env[key] = value
        block = toml_format_server(
            name,
            command=cfg.get("command"),
            args=list(cfg.get("args") or []) or None,
            url=cfg.get("url"),
            env=env,
            disabled=disabled,
            extra_lines=self._extra_keys(cfg),
        )
        new = toml_remove_sections(text, f"mcp_servers.{name}")
        new = toml_remove_sections(new, f"mcp_servers_disabled.{name}")
        new = toml_upsert_server_block(new, name, block, disabled=disabled)
        self._write(path, new, text, secret=True)


def _reindent_block(block: str, indent: str) -> str:
    """Prefix continuation lines so a multi-line JSON value sits inside a parent object."""
    lines = block.splitlines()
    if not lines:
        return block
    out = [lines[0]]
    for line in lines[1:]:
        out.append((indent + line) if line.strip() else line)
    return "\n".join(out)


def _tilde_path(p: Path) -> str:
    try:
        return "~/" + str(p.resolve().relative_to(home()))
    except ValueError:
        return str(p)


def _env_var_name(ref: str) -> str:
    ref = ref.strip()
    m = re.match(r"^\$\{([^}:]+)(?::-?[^}]*)?\}$", ref)
    if m:
        return m.group(1)
    if ref.startswith("$"):
        return ref[1:]
    return ref


def make_harnesses(paths: Paths, dry_run: bool = False) -> dict[str, Harness]:
    return {
        "opencode": OpenCodeHarness(paths, dry_run),
        "cursor": CursorHarness(paths, dry_run),
        "codex": CodexHarness(paths, dry_run),
    }


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def load_catalog(paths: Paths) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    dirs: list[Path] = []
    d = paths.catalog_dir
    if d.is_dir():
        dirs.append(d)
        mcp_sub = d / "mcp"
        if mcp_sub.is_dir():
            dirs.append(mcp_sub)
    for folder in dirs:
        for f in sorted(folder.glob("*.jsonc")) + sorted(folder.glob("*.json")):
            try:
                data = load_jsonc(f.read_text(encoding="utf-8"))
            except Exception as e:
                warn(f"catalog {f.name}: {e}")
                continue
            name = data.get("name") or f.stem
            data["name"] = name
            out[name] = data
    return out


def catalog_to_spec(entry: dict[str, Any], *, enabled: bool = True) -> ServerSpec:
    stype = entry.get("type", "stdio")
    command = entry.get("command")
    if isinstance(command, str):
        command = os.path.expandvars(os.path.expanduser(command))
    args = [os.path.expandvars(os.path.expanduser(str(a))) for a in (entry.get("args") or [])]
    env: dict[str, str] = {}
    for k in entry.get("env_keys") or []:
        env[str(k)] = "PLACEHOLDER"
    for k, v in (entry.get("env") or {}).items():
        if not is_secret_key(str(k)):
            env[str(k)] = str(v)
    return ServerSpec(
        name=entry["name"],
        type=stype,
        command=command,
        args=args,
        env=env,
        url=entry.get("url"),
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# Skills (symlink-first) + manifest + profiles
# ---------------------------------------------------------------------------

def _parse_keep_from_sync_script(script: Path) -> list[str] | None:
    if not script.is_file():
        return None
    text = script.read_text(encoding="utf-8")
    m = re.search(r"KEEP=\((.*?)\)", text, re.S)
    if not m:
        return None
    return re.findall(r"[A-Za-z0-9_./-]+", m.group(1))


def load_skills_keep(paths: Paths, override: Sequence[str] | None = None) -> list[str]:
    if override is not None:
        return list(override)
    if paths.skills_manifest.is_file():
        names = []
        for line in paths.skills_manifest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            names.append(line.split()[0])
        if names:
            return names
    parsed = _parse_keep_from_sync_script(paths.sync_skills)
    if parsed:
        return parsed
    die(f"no skills manifest at {paths.skills_manifest} and cannot parse {paths.sync_skills}")


def _skill_entries(dest: Path) -> list[str]:
    if not dest.is_dir():
        return []
    return sorted(
        p.name
        for p in dest.iterdir()
        if p.name != ".system" and not p.name.startswith(".")
    )


@dataclass
class SkillDestPlan:
    label: str
    dest: Path
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    relink: list[str] = field(default_factory=list)


def plan_skills(paths: Paths, keep: Sequence[str]) -> list[SkillDestPlan]:
    want = set(keep)
    plans: list[SkillDestPlan] = []
    for label, dest in paths.skill_dests().items():
        p = SkillDestPlan(label=label, dest=dest)
        if not dest.is_dir():
            p.missing = list(keep)
            plans.append(p)
            continue
        current = set(_skill_entries(dest))
        p.missing = sorted(want - current)
        p.extra = sorted(current - want)
        for name in sorted(want & current):
            link = dest / name
            if link.is_symlink() and not link.exists():
                p.broken.append(name)
            elif link.is_symlink():
                target = Path(os.readlink(link))
                expected = Path(os.path.relpath(paths.agents_skills / name, dest))
                if target != expected and (dest / target).resolve() != (paths.agents_skills / name).resolve():
                    p.relink.append(name)
            elif not link.is_symlink():
                p.relink.append(name)
        plans.append(p)
    return plans


def sync_skills_native(
    paths: Paths,
    keep: Sequence[str],
    *,
    prune: bool = False,
    dry_run: bool = False,
) -> list[SkillDestPlan]:
    """Symlink KEEP skills from ~/.agents/skills into each harness. Preserve Codex .system."""
    if not paths.agents_skills.is_dir():
        die(f"canonical skills missing: {paths.agents_skills}")
    for name in keep:
        if not (paths.agents_skills / name).is_dir():
            die(f"missing canonical skill: {paths.agents_skills / name}")

    plans = plan_skills(paths, keep)
    for p in plans:
        print(f"## skills → {p.label} ({p.dest})")
        if dry_run:
            relink = p.relink + p.broken
            print(
                f"  dry-run: create={p.missing or '-'} relink={relink or '-'} "
                f"extra={p.extra or '-'} prune={prune}"
            )
            continue
        p.dest.mkdir(parents=True, exist_ok=True)
        # Cursor/OpenCode: wipe unmanaged entries only when prune; always ensure KEEP links
        # Codex: never wipe whole dir (.system)
        for name in keep:
            src = paths.agents_skills / name
            link = p.dest / name
            rel = os.path.relpath(src, p.dest)
            need = (
                name in p.missing
                or name in p.broken
                or name in p.relink
                or not link.exists()
            )
            if need:
                if link.exists() or link.is_symlink():
                    if link.is_dir() and not link.is_symlink():
                        shutil.rmtree(link)
                    else:
                        link.unlink(missing_ok=True)
                link.symlink_to(rel)
                print(f"  + {name} -> {rel}")
        if prune:
            for name in p.extra:
                victim = p.dest / name
                if victim.is_symlink() or victim.is_file():
                    victim.unlink()
                elif victim.is_dir():
                    shutil.rmtree(victim)
                print(f"  - prune {name}")
        # verify
        broken = [n for n in keep if not (p.dest / n).exists()]
        if broken:
            die(f"{p.label}: broken after sync: {broken}")
    return plans


def manifest_path() -> Path:
    return tool_config_dir() / "manifest.json"


def load_manifest() -> dict[str, Any]:
    p = manifest_path()
    if not p.is_file():
        return {"version": 1, "mcp": {}, "skills": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "mcp": {}, "skills": {}}


def save_manifest(data: dict[str, Any]) -> None:
    data = dict(data)
    data["version"] = 1
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def snapshot_mcp_manifest(paths: Paths) -> dict[str, Any]:
    harnesses = make_harnesses(paths)
    ignore, _ = _doctor_config()
    out: dict[str, Any] = {}
    for hname, h in harnesses.items():
        if not h.available():
            continue
        for s in h.list_servers():
            if s.name in ignore:
                continue
            entry = out.setdefault(s.name, {"present": [], "enabled": {}, "token_digest": {}})
            entry["present"].append(hname)
            entry["enabled"][hname] = bool(s.enabled)
            _, val = h.token_info(s.name)
            dig = token_hash(val)
            if dig:
                entry["token_digest"][hname] = dig
    return out


def update_manifest_after_sync(paths: Paths, keep: Sequence[str]) -> None:
    man = load_manifest()
    man["mcp"] = snapshot_mcp_manifest(paths)
    keep_list = list(keep)
    man["skills"] = {
        "names": keep_list,
        "keep_hash": hashlib.sha256("\n".join(keep_list).encode()).hexdigest()[:16],
        "dests": {
            label: {
                "count": len(_skill_entries(dest)),
                "names_hash": hashlib.sha256(
                    "\n".join(_skill_entries(dest)).encode()
                ).hexdigest()[:12],
            }
            for label, dest in paths.skill_dests().items()
        },
    }
    save_manifest(man)
    print(f"manifest: updated {manifest_path()}")


def load_profiles(paths: Paths) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    d = paths.profiles_dir
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.jsonc")) + sorted(d.glob("*.json")):
        try:
            data = load_jsonc(f.read_text(encoding="utf-8"))
        except Exception as e:
            warn(f"profile {f.name}: {e}")
            continue
        name = data.get("name") or f.stem
        data["name"] = name
        out[name] = data
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> None:
    paths = Paths.load()
    harnesses = make_harnesses(paths)
    wanted = parse_harness_list(args.harness)
    # collect all server names
    names: set[str] = set()
    states: dict[str, dict[str, ServerState]] = {h: {} for h in wanted}
    for hname in wanted:
        h = harnesses[hname]
        if not h.available():
            print(f"# {hname}: config missing ({h.config_path})", file=sys.stderr)
            continue
        for spec in h.list_servers():
            names.add(spec.name)
            src, val = h.token_info(spec.name)
            states[hname][spec.name] = ServerState(
                harness=hname,
                name=spec.name,
                present=True,
                enabled=spec.enabled,
                spec=spec,
                token_source=src,
                token_preview=mask_secret(val) if val else None,
                token_digest=token_hash(val),
            )

    if not names:
        print("No MCP servers found.")
        return

    cols = ["server"] + list(wanted)
    widths = {c: len(c) for c in cols}
    rows: list[dict[str, str]] = []
    for name in sorted(names):
        row = {"server": name}
        for hname in wanted:
            st = states[hname].get(name)
            if not st:
                cell = "—"
            elif st.enabled:
                cell = "active"
            else:
                cell = "off"
            row[hname] = cell
            widths[hname] = max(widths[hname], len(cell))
        widths["server"] = max(widths["server"], len(name))
        rows.append(row)

    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("  ".join("-" * widths[c] for c in cols))
    for row in rows:
        print("  ".join(row[c].ljust(widths[c]) for c in cols))


def cmd_list(args: argparse.Namespace) -> None:
    paths = Paths.load()
    harnesses = make_harnesses(paths)
    wanted = parse_harness_list(args.harness)
    for hname in wanted:
        h = harnesses[hname]
        print(f"## {hname} ({h.config_path})")
        if not h.available():
            print("  (missing)\n")
            continue
        servers = h.list_servers()
        if not servers:
            print("  (none)\n")
            continue
        for s in servers:
            src, val = h.token_info(s.name)
            flag = "on" if s.enabled else "off"
            print(f"  {s.name} [{flag}] type={s.type}")
            if s.command:
                print(f"    command: {s.command}")
            if s.args:
                print(f"    args: {s.args}")
            if s.url:
                print(f"    url: {s.url}")
            if s.env:
                print(f"    env: {mask_env(s.env)}")
            print(f"    token: source={src} value={mask_secret(val) if val else '(none)'}")
        print()


def cmd_mcp_add(args: argparse.Namespace) -> None:
    paths = Paths.load()
    dry = bool(args.dry_run)
    harnesses = make_harnesses(paths, dry)
    wanted = parse_harness_list(args.harness)

    if args.from_catalog:
        cat = load_catalog(paths)
        if args.name not in cat:
            die(f"not in catalog: {args.name}. Known: {', '.join(cat) or '(empty)'}")
        spec = catalog_to_spec(cat[args.name], enabled=True)
        if args.command:
            spec.command = args.command
        if args.args:
            spec.args = list(args.args)
        if args.url:
            spec.url = args.url
        env = parse_env_pairs(args.env)
        spec.env.update(env)
    else:
        if not args.type:
            die("--type stdio|remote required (or use --from-catalog)")
        if args.type == "stdio" and not args.command:
            die("--command required for stdio")
        if args.type == "remote" and not args.url:
            die("--url required for remote")
        spec = ServerSpec(
            name=args.name,
            type=args.type,
            command=args.command,
            args=list(args.args or []),
            env=parse_env_pairs(args.env),
            url=args.url,
            enabled=True,
        )

    for hname in wanted:
        h = harnesses[hname]
        if not h.available() and hname != "cursor":
            # allow creating cursor mcp.json; for others require existing file
            if hname == "opencode":
                warn(f"{hname}: config missing, skip")
                continue
            if hname == "codex":
                warn(f"{hname}: config missing, skip")
                continue
        print(f"+ add {spec.name} -> {hname}")
        h.add(spec)

    secret_keys = [k for k in spec.env if is_secret_key(k)]
    if secret_keys or args.from_catalog:
        print("Next: set token locally, e.g.")
        print(f"  mcp-ctrl token set {spec.name} <value|file>")


def cmd_mcp_remove(args: argparse.Namespace) -> None:
    paths = Paths.load()
    harnesses = make_harnesses(paths, bool(args.dry_run))
    for hname in parse_harness_list(args.harness):
        h = harnesses[hname]
        if not h.available():
            warn(f"{hname}: missing, skip")
            continue
        print(f"- remove {args.name} @ {hname}")
        h.remove(args.name)


def cmd_mcp_enable(args: argparse.Namespace, enabled: bool) -> None:
    paths = Paths.load()
    harnesses = make_harnesses(paths, bool(args.dry_run))
    for hname in parse_harness_list(args.harness):
        h = harnesses[hname]
        if not h.available():
            warn(f"{hname}: missing, skip")
            continue
        print(f"{'enable' if enabled else 'disable'} {args.name} @ {hname}")
        h.set_enabled(args.name, enabled)


def cmd_token_source(args: argparse.Namespace) -> None:
    paths = Paths.load()
    harnesses = make_harnesses(paths)
    for hname in parse_harness_list(args.harness):
        h = harnesses[hname]
        if not h.available():
            print(f"{hname}: (missing)")
            continue
        src, val = h.token_info(args.server)
        print(f"{hname}: source={src} value={mask_secret(val) if val else '(none)'} digest={token_hash(val) or '-'}")


def cmd_token_set(args: argparse.Namespace) -> None:
    paths = Paths.load()
    raw = args.value
    if Path(raw).expanduser().is_file():
        value = Path(raw).expanduser().read_text(encoding="utf-8").strip()
    else:
        value = raw
    if not value:
        die("empty token")
    harnesses = make_harnesses(paths, bool(args.dry_run))
    for hname in parse_harness_list(args.harness):
        h = harnesses[hname]
        if not h.available():
            warn(f"{hname}: missing, skip")
            continue
        if not h.get(args.server):
            warn(f"{hname}: {args.server} not present, skip")
            continue
        print(f"token set {args.server} @ {hname}")
        h.token_set(args.server, value)


def cmd_catalog_list(_args: argparse.Namespace) -> None:
    paths = Paths.load()
    cat = load_catalog(paths)
    if not cat:
        print(f"Catalog empty ({paths.catalog_dir})")
        return
    for name, e in cat.items():
        print(f"{name}: {e.get('description', '')}")
        print(f"  type={e.get('type')} command={e.get('command')} url={e.get('url')}")
        print(f"  env_keys={e.get('env_keys') or []}")


def cmd_skills_list(_args: argparse.Namespace) -> None:
    paths = Paths.load()
    keep = load_skills_keep(paths)
    print(f"KEEP ({len(keep)}) from {paths.skills_manifest if paths.skills_manifest.is_file() else paths.sync_skills}")
    for name in keep:
        ok = (paths.agents_skills / name).is_dir()
        print(f"  {'✓' if ok else '✗'} {name}")
    print("\nHarness links:")
    for plan in plan_skills(paths, keep):
        cur = _skill_entries(plan.dest)
        print(
            f"  {plan.label}: {len(cur)} "
            f"missing={plan.missing or '-'} extra={plan.extra or '-'} broken={plan.broken or '-'}"
        )


def cmd_skills_sync(args: argparse.Namespace) -> None:
    paths = Paths.load()
    keep = load_skills_keep(paths)
    prune = bool(getattr(args, "prune", False))
    dry = bool(args.dry_run)
    print(f"skills sync ({'prune' if prune else 'safe'}) keep={len(keep)} canonical={paths.agents_skills}")
    sync_skills_native(paths, keep, prune=prune, dry_run=dry)
    if not dry:
        update_manifest_after_sync(paths, keep)
        print(f"OK: synced {len(keep)} skills → cursor + opencode + codex")


def cmd_plan(args: argparse.Namespace) -> None:
    """Show MCP + skills sync plan (vsync-style), no writes."""
    paths = Paths.load()
    harnesses = make_harnesses(paths)
    ignore, optional = _doctor_config()
    man = load_manifest()
    wanted = parse_harness_list(getattr(args, "harness", None))

    print("📋 Sync Plan (read-only)\n")
    print("## MCP")
    by_h: dict[str, dict[str, ServerSpec]] = {}
    all_names: set[str] = set()
    active = []
    for hname in wanted:
        h = harnesses[hname]
        by_h[hname] = {}
        if not h.available():
            print(f"  {hname}: config missing — skip")
            continue
        active.append(hname)
        for s in h.list_servers():
            by_h[hname][s.name] = s
            all_names.add(s.name)

    mcp_actions = 0
    for name in sorted(all_names):
        if name in ignore:
            continue
        have = [h for h in active if name in by_h[h]]
        missing = [h for h in active if name not in by_h[h]]
        if missing and have:
            tag = "optional" if name in optional else "CREATE"
            print(f"  [{tag}] mcp/{name}: present in {have}, missing in {missing}")
            if name not in optional:
                mcp_actions += 1
        digests = {}
        for hname in have:
            _, val = harnesses[hname].token_info(name)
            if val:
                digests[hname] = token_hash(val)
        if len(set(digests.values())) > 1:
            print(f"  [UPDATE] mcp/{name}: token digest drift {digests}")
            mcp_actions += 1
        prev = (man.get("mcp") or {}).get(name, {})
        prev_dig = prev.get("token_digest") or {}
        if prev_dig and digests and prev_dig != digests:
            print(f"  [drift] mcp/{name}: changed since last manifest")

    if mcp_actions == 0:
        print("  (no required MCP actions)")

    print("\n## Skills")
    keep = load_skills_keep(paths)
    skill_actions = 0
    for p in plan_skills(paths, keep):
        bits = []
        if p.missing:
            bits.append(f"CREATE {p.missing}")
            skill_actions += len(p.missing)
        if p.broken or p.relink:
            bits.append(f"RELINK {p.broken + p.relink}")
            skill_actions += len(p.broken) + len(p.relink)
        if p.extra:
            bits.append(f"EXTRA {p.extra} (use --prune to delete)")
        print(f"  {p.label}: {'; '.join(bits) if bits else 'ok'}")

    prev_skills = (man.get("skills") or {}).get("keep_hash")
    cur_hash = hashlib.sha256("\n".join(keep).encode()).hexdigest()[:16]
    if prev_skills and prev_skills != cur_hash:
        print(f"  manifest: KEEP hash changed ({prev_skills} → {cur_hash})")

    print("\n────────────────────────────────────────")
    print(f"Required actions: mcp≈{mcp_actions}, skills≈{skill_actions}")
    print("Apply skills:  mcp-ctrl skills sync [--prune]")
    print("Fix MCP gaps:  mcp-ctrl doctor --fix")
    print("Or profile:    mcp-ctrl profile apply <name>")


def cmd_profile_list(_args: argparse.Namespace) -> None:
    paths = Paths.load()
    profiles = load_profiles(paths)
    if not profiles:
        print(f"No profiles in {paths.profiles_dir}")
        return
    for name, p in profiles.items():
        print(f"{name}: {p.get('description', '')}")
        print(f"  mcp={p.get('mcp') or []} optional_mcp={p.get('optional_mcp') or []}")
        sk = p.get("skills")
        print(f"  skills={'all(manifest)' if sk is None else sk}")


def cmd_profile_apply(args: argparse.Namespace) -> None:
    paths = Paths.load()
    profiles = load_profiles(paths)
    name = args.name
    if name not in profiles:
        die(f"unknown profile {name!r}. Known: {', '.join(profiles) or '(none)'}")
    profile = profiles[name]
    dry = bool(args.dry_run)
    prune = bool(args.prune)
    harnesses = make_harnesses(paths, dry)
    wanted = parse_harness_list(args.harness)
    cat = load_catalog(paths)

    print(f"profile apply {name} ({'dry-run' if dry else 'live'})")

    # MCP required
    for mcp_name in profile.get("mcp") or []:
        if mcp_name not in cat:
            warn(f"mcp {mcp_name} not in catalog — skip add")
            continue
        spec = catalog_to_spec(cat[mcp_name], enabled=True)
        for hname in wanted:
            h = harnesses[hname]
            if not h.available() and hname != "cursor":
                warn(f"{hname}: missing, skip mcp/{mcp_name}")
                continue
            existing = h.get(mcp_name) if h.available() else None
            if existing:
                print(f"  mcp/{mcp_name} @ {hname}: already present")
                continue
            print(f"  + mcp/{mcp_name} -> {hname}")
            if not dry:
                h.add(spec)

    # optional: add only if --with-optional
    if getattr(args, "with_optional", False):
        for mcp_name in profile.get("optional_mcp") or []:
            if mcp_name not in cat:
                continue
            spec = catalog_to_spec(cat[mcp_name], enabled=False)
            for hname in wanted:
                h = harnesses[hname]
                if not h.available():
                    continue
                if h.get(mcp_name):
                    continue
                print(f"  + optional mcp/{mcp_name} -> {hname} (disabled)")
                if not dry:
                    h.add(spec)

    # skills
    sk = profile.get("skills")
    keep = load_skills_keep(paths, override=sk if isinstance(sk, list) else None)
    full_keep = load_skills_keep(paths)
    if prune and isinstance(sk, list) and set(keep) != set(full_keep):
        warn(
            "prune + subset skills would delete skills outside this profile; "
            "refusing prune (pass full KEEP profile or sync without --prune)"
        )
        prune = False
    print(f"  skills keep={len(keep)} prune={prune}")
    sync_skills_native(paths, keep, prune=prune, dry_run=dry)
    if not dry:
        update_manifest_after_sync(paths, keep if sk is None else load_skills_keep(paths))

    needs_token = [
        m for m in (profile.get("mcp") or [])
        if m in cat and (cat[m].get("env_keys") or [])
    ]
    if needs_token:
        print("Next (tokens, local only):")
        for m in needs_token:
            print(f"  mcp-ctrl token set {m} <value|file>")


def _doctor_config() -> tuple[set[str], set[str]]:
    """Return (ignore_servers, optional_servers).

    ignore: never compared across harnesses (platform-bundled).
    optional: may be missing in some harnesses — reported as info, not issue.
    """
    ignore = {"node_repl"}
    optional = {"figma"}
    cfg_path = tool_config_dir() / "config.jsonc"
    if cfg_path.is_file():
        try:
            data = load_jsonc(cfg_path.read_text(encoding="utf-8"))
            for x in data.get("doctor_ignore_servers") or []:
                ignore.add(str(x))
            for x in data.get("doctor_optional_servers") or []:
                optional.add(str(x))
        except Exception:
            pass
    return ignore, optional


def cmd_doctor(args: argparse.Namespace) -> None:
    paths = Paths.load()
    harnesses = make_harnesses(paths, dry_run=False)
    issues: list[str] = []
    infos: list[str] = []
    ignore, optional = _doctor_config()

    present = {n: h.available() for n, h in harnesses.items()}
    for n, ok in present.items():
        if not ok:
            issues.append(f"harness {n}: config missing ({harnesses[n].config_path})")

    # server matrix
    all_names: set[str] = set()
    by_h: dict[str, dict[str, ServerSpec]] = {}
    for n, h in harnesses.items():
        by_h[n] = {}
        if not h.available():
            continue
        for s in h.list_servers():
            by_h[n][s.name] = s
            all_names.add(s.name)

    active_harnesses = [n for n, ok in present.items() if ok]
    for name in sorted(all_names):
        if name in ignore:
            infos.append(f"server {name}: ignored (harness-specific)")
            continue
        have = [h for h in active_harnesses if name in by_h[h]]
        missing = [h for h in active_harnesses if name not in by_h[h]]
        if missing and have:
            msg = f"server {name}: present in {have}, missing in {missing}"
            if name in optional:
                infos.append(msg + " (optional)")
            else:
                issues.append(msg)

        # token consistency among those that have secret keys
        digests: dict[str, str] = {}
        for hname in have:
            h = harnesses[hname]
            src, val = h.token_info(name)
            spec = by_h[hname][name]
            if not spec.token_keys() and src == "missing":
                continue
            # Cursor ${VAR} without env
            if hname == "cursor" and src == "env" and val is None:
                issues.append(f"cursor/{name}: env-ref token but variable unset (401 risk)")
            if hname == "opencode" and src == "file" and val is None:
                issues.append(f"opencode/{name}: {{file:}} secret missing or unreadable")
            if val is not None:
                digests[hname] = token_hash(val) or ""
        if len(set(digests.values())) > 1:
            issues.append(f"server {name}: token mismatch across {digests}")

        # enabled drift (informational if intentional like figma)
        en = {h: by_h[h][name].enabled for h in have}
        if len(set(en.values())) > 1:
            infos.append(f"server {name}: enabled differs {en}")

    # skills
    try:
        keep = load_skills_keep(paths)
    except SystemExit:
        keep = []
        issues.append("skills: cannot load KEEP manifest")
    if keep:
        for p in plan_skills(paths, keep):
            if not p.dest.is_dir():
                issues.append(f"skills/{p.label}: dir missing")
                continue
            for name in p.broken:
                issues.append(f"skills/{p.label}: broken symlink {name}")
            if p.missing:
                issues.append(f"skills/{p.label}: missing {p.missing}")
            if p.extra:
                infos.append(f"skills/{p.label}: extra {p.extra} (skills sync --prune)")

    # manifest drift
    man = load_manifest()
    snap = snapshot_mcp_manifest(paths)
    for name, cur in snap.items():
        prev = (man.get("mcp") or {}).get(name)
        if not prev:
            infos.append(f"manifest: mcp/{name} not recorded yet (run skills sync or profile apply)")
            continue
        if prev.get("token_digest") and cur.get("token_digest") and prev["token_digest"] != cur["token_digest"]:
            issues.append(f"manifest: mcp/{name} token digest changed since last save")

    print("## doctor")
    if infos:
        print("info:")
        for i in infos:
            print(f"  - {i}")
    if issues:
        print("issues:")
        for i in issues:
            print(f"  - {i}")
        if args.fix:
            print("\n--fix: applying safe fixes")
            fixers = make_harnesses(paths, False)
            for name in sorted(all_names):
                if name in ignore or name in optional:
                    continue
                donors = [h for h in active_harnesses if name in by_h[h]]
                if not donors:
                    continue
                donor_spec = by_h[donors[0]][name]
                for hname in active_harnesses:
                    if name in by_h[hname]:
                        continue
                    print(f"  fix: add {name} -> {hname}")
                    fixers[hname].add(donor_spec)
            if keep:
                print("  fix: skills sync (safe)")
                sync_skills_native(paths, keep, prune=False, dry_run=False)
            update_manifest_after_sync(paths, keep or [])
            print("Re-run doctor to verify. Tokens may still need: mcp-ctrl token set ...")
        raise SystemExit(1)
    print("ok: harness MCP + skills look consistent")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mcp-ctrl",
        description="Universal remote for MCP servers & skills (OpenCode / Cursor / Codex)",
    )
    p.add_argument("--version", action="version", version=f"mcp-ctrl {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="table: server x harness x state")
    sp.add_argument("--harness", default="all")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("list", help="detailed list")
    sp.add_argument("harness_pos", nargs="?", default=None)
    sp.add_argument("--harness", default=None)
    sp.set_defaults(func=_cmd_list_wrap)

    mcp = sub.add_parser("mcp", help="manage MCP servers")
    msub = mcp.add_subparsers(dest="mcp_cmd", required=True)

    sp = msub.add_parser("add", help="add server")
    sp.add_argument("name")
    sp.add_argument("--harness", default="all")
    sp.add_argument("--type", choices=("stdio", "remote"))
    sp.add_argument("--command")
    sp.add_argument("--args", nargs="*", default=None)
    sp.add_argument("--env", action="append", default=[])
    sp.add_argument("--url")
    sp.add_argument("--from-catalog", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_mcp_add)

    sp = msub.add_parser("remove", help="remove server")
    sp.add_argument("name")
    sp.add_argument("--harness", default="all")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_mcp_remove)

    sp = msub.add_parser("enable", help="enable server")
    sp.add_argument("name")
    sp.add_argument("--harness", default="all")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=lambda a: cmd_mcp_enable(a, True))

    sp = msub.add_parser("disable", help="disable server")
    sp.add_argument("name")
    sp.add_argument("--harness", default="all")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=lambda a: cmd_mcp_enable(a, False))

    tok = sub.add_parser("token", help="token helpers")
    tsub = tok.add_subparsers(dest="token_cmd", required=True)

    sp = tsub.add_parser("set", help="set token everywhere")
    sp.add_argument("server")
    sp.add_argument("value", help="literal value or path to file")
    sp.add_argument("--harness", default="all")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_token_set)

    sp = tsub.add_parser("source", help="show where token lives")
    sp.add_argument("server")
    sp.add_argument("--harness", default="all")
    sp.set_defaults(func=cmd_token_source)

    sk = sub.add_parser("skills", help="skills helpers (symlink-first from ~/.agents/skills)")
    sksub = sk.add_subparsers(dest="skills_cmd", required=True)
    sp = sksub.add_parser("list", help="list KEEP + harness link status")
    sp.set_defaults(func=cmd_skills_list)
    sp = sksub.add_parser("sync", help="symlink KEEP into all harnesses")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument(
        "--prune",
        action="store_true",
        help="delete extras in targets not in KEEP (safe mode is default)",
    )
    sp.set_defaults(func=cmd_skills_sync)

    sp = sub.add_parser("plan", help="show MCP+skills sync plan (no writes)")
    sp.add_argument("--harness", default="all")
    sp.set_defaults(func=cmd_plan)

    prof = sub.add_parser("profile", help="apply named MCP+skills profiles")
    psub = prof.add_subparsers(dest="profile_cmd", required=True)
    sp = psub.add_parser("list", help="list profiles")
    sp.set_defaults(func=cmd_profile_list)
    sp = psub.add_parser("apply", help="add profile MCP from catalog + sync skills")
    sp.add_argument("name")
    sp.add_argument("--harness", default="all")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--prune", action="store_true", help="prune skills extras")
    sp.add_argument(
        "--with-optional",
        action="store_true",
        help="also add optional_mcp (usually disabled)",
    )
    sp.set_defaults(func=cmd_profile_apply)

    sp = sub.add_parser("doctor", help="consistency audit")
    sp.add_argument("--fix", action="store_true")
    sp.set_defaults(func=cmd_doctor)

    cat = sub.add_parser("catalog", help="shared MCP catalog")
    csub = cat.add_subparsers(dest="catalog_cmd", required=True)
    sp = csub.add_parser("list", help="list catalog entries")
    sp.set_defaults(func=cmd_catalog_list)

    return p


def _cmd_list_wrap(args: argparse.Namespace) -> None:
    if args.harness is None and args.harness_pos:
        args.harness = args.harness_pos
    elif args.harness is None:
        args.harness = "all"
    cmd_list(args)


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
