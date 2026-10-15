"""MCP server (stdio) that hands a `claude -p` process rig's tools for one hand.

Claude Code starts this script from the --mcp-config that `ClaudeCodeWorker` writes. Every
tools/list and tools/call is forwarded over a localhost socket to the rig process, which runs
the hand's own Toolbox (workspace limits, `run` allow list, `delegate`, ...). Standard library
only, and it doesn't import rig, so it runs with whatever Python started rig.

    python ccbridge.py <port> <token-file>
"""

import json
import socket
import sys
import threading

PROTOCOL = "2025-06-18"
write_lock = threading.Lock()


def send(msg: dict) -> None:
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    with write_lock:
        sys.stdout.buffer.write(line.encode("utf-8"))
        sys.stdout.buffer.flush()


def ask_rig(port: int, token: str, request: dict) -> dict:
    """One request per connection: a JSON line out, a JSON line back."""
    with socket.create_connection(("127.0.0.1", port)) as conn:
        conn.sendall((json.dumps({**request, "token": token}) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode("utf-8"))


def handle(msg: dict, port: int, token: str) -> None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return  # a notification (notifications/initialized, cancelled, ...): nothing to answer
    try:
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "rig", "version": "1"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": ask_rig(port, token, {"op": "list"})["tools"]}
        elif method == "tools/call":
            answer = ask_rig(port, token, {"op": "call", "name": params.get("name"),
                                           "input": params.get("arguments") or {}})
            result = {"content": [{"type": "text", "text": answer["content"]}], "isError": answer["is_error"]}
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})
            return
    except Exception as e:  # rig went away, bad JSON, ...: tell the model instead of hanging
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}})
        return
    send({"jsonrpc": "2.0", "id": mid, "result": result})


def main() -> None:
    port = int(sys.argv[1])
    with open(sys.argv[2], encoding="utf-8") as f:
        token = f.read().strip()
    for raw in sys.stdin.buffer:
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(msg, dict):
            # Tool calls can take long (`delegate` runs a whole hand), so each gets its own thread.
            threading.Thread(target=handle, args=(msg, port, token), daemon=True).start()


if __name__ == "__main__":
    main()
