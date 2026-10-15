"""Stands in for `claude -p` in tests/test_claude_code.py.

Like Claude Code it starts the MCP server named in --mcp-config and talks MCP to it over stdio:
initialize, tools/list, then one tools/call per FAKE_CALLS entry. It prints a JSON result like
`claude -p --output-format json`, whose `result` reports what it saw. FAKE_RESULT, if set, is
printed instead (to test error results); FAKE_SLEEP makes it hang (to test stopping).
"""

import json
import os
import subprocess
import sys
import time

args = sys.argv[1:]
prompt = sys.stdin.read()
if os.environ.get("FAKE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SLEEP"]))
if os.environ.get("FAKE_RESULT"):
    print(os.environ["FAKE_RESULT"])
    sys.exit(1)

config = json.load(open(args[args.index("--mcp-config") + 1], encoding="utf-8"))
server = config["mcpServers"]["rig"]
proc = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE)


def rpc(mid, method, params=None):
    proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}) + "\n").encode())
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


init = rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
proc.stdin.write(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}\n')
proc.stdin.flush()
tools = rpc(2, "tools/list")["result"]["tools"]
calls = [rpc(3 + i, "tools/call", {"name": c["name"], "arguments": c["input"]})
         for i, c in enumerate(json.loads(os.environ.get("FAKE_CALLS", "[]")))]
proc.stdin.close()
proc.wait(timeout=10)

seen = {
    "prompt": prompt,
    "args": args,
    "api_key": "ANTHROPIC_API_KEY" in os.environ,
    "server": init["result"]["serverInfo"]["name"],
    "tools": [t["name"] for t in tools],
    "schemas": all("inputSchema" in t for t in tools),
    "calls": [c.get("result") or c.get("error") for c in calls],
}
out = {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(seen),
       "session_id": "sess-1", "num_turns": 2,
       "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50}}
if "--json-schema" in args:
    out["structured_output"] = {"ok": True}
print(json.dumps(out))
