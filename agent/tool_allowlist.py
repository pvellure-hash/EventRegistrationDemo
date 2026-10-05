"""agent/tool_allowlist.py - Phase 1: MCP / tool allow-list enforcement.

Closes Issue E5 (an unidentified Atlassian MCP connection possibly adding
token overhead on every call). The pipeline must only ever load tools that
are explicitly declared here; anything else present in the live Copilot CLI
configuration fails the pre-flight check rather than silently running.

.env:
    ALLOWED_MCP_SERVERS=        # comma-separated server names, blank = none allowed
"""
import os
import json
import glob

DEFAULT_CONFIG_GLOBS = [
    os.path.expanduser("~/.copilot/mcp-config.json"),
    os.path.expanduser("~/.config/copilot/mcp-config.json"),
]


def allowed_servers():
    raw = os.getenv("ALLOWED_MCP_SERVERS", "").strip()
    return {s.strip() for s in raw.split(",") if s.strip()}


def _find_config():
    for path in DEFAULT_CONFIG_GLOBS:
        if os.path.exists(path):
            return path
    for path in glob.glob(os.path.expanduser("~/.copilot/*.json")):
        return path
    return None


def check(config_path=None):
    """Returns (ok: bool, detail: str). ok=True with no config file found is
    treated as a pass (nothing loaded = nothing to leak), but is reported
    explicitly so it is visible rather than silently assumed."""
    allowed = allowed_servers()
    path = config_path or _find_config()
    if not path or not os.path.exists(path):
        return True, "no MCP config file found - no additional tools loaded"
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        return False, f"could not read MCP config at {path}: {e}"
    servers = set((cfg.get("mcpServers") or cfg.get("servers") or {}).keys())
    unknown = servers - allowed
    if unknown:
        return False, (f"unexpected MCP server(s) configured: {', '.join(sorted(unknown))} "
                       f"(allowed: {', '.join(sorted(allowed)) or 'none'}). "
                       f"Every run would silently load these tools/pay their token overhead.")
    if servers:
        return True, f"MCP servers loaded: {', '.join(sorted(servers))} (all allow-listed)"
    return True, "MCP config present but no servers configured"
