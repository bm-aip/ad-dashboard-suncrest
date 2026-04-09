"""
Direct TrueClicks MCP SSE client.
Bypasses Anthropic API entirely — connects directly to TrueClicks MCP server.
"""
import json
import queue
import threading
import uuid
import requests


def call_trueclicks_gaql(mcp_url, customer_id, login_customer_id, gaql_query, timeout=30):
    """
    Call TrueClicks MCP server directly via SSE transport.
    Returns parsed MCP tool result content, or None on failure.
    """
    result_q   = queue.Queue()
    post_url   = [None]
    session_id = str(uuid.uuid4())

    def sse_reader():
        try:
            with requests.get(
                mcp_url,
                stream=True,
                headers={
                    "Accept":        "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection":    "keep-alive",
                },
                timeout=timeout,
            ) as resp:
                event_type = None
                for raw in resp.iter_lines():
                    if raw is None:
                        continue
                    line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                    if not line:
                        event_type = None
                        continue
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                        continue
                    if line.startswith("data:"):
                        raw_data = line[5:].strip()
                        try:
                            data = json.loads(raw_data)
                        except Exception:
                            continue

                        # Endpoint event — gives us the POST URL
                        if event_type == "endpoint" or (
                            isinstance(data, dict) and "uri" in data and "jsonrpc" not in data
                        ):
                            uri = data.get("uri") or data.get("url", "")
                            if uri:
                                post_url[0] = uri
                                result_q.put(("endpoint", uri))
                            continue

                        # JSON-RPC response
                        if isinstance(data, dict) and "jsonrpc" in data:
                            msg_id = data.get("id")
                            if "error" in data:
                                result_q.put(("error", data["error"]))
                            elif "result" in data:
                                result_q.put(("result", {"id": msg_id, "result": data["result"]}))
        except Exception as exc:
            result_q.put(("sse_error", str(exc)))

    # Start SSE reader in background thread
    t = threading.Thread(target=sse_reader, daemon=True)
    t.start()

    def post(payload):
        try:
            requests.post(
                post_url[0],
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
        except Exception as exc:
            print(f"[TrueClicks Direct] POST error: {exc}")

    def wait(label, secs=8):
        try:
            kind, data = result_q.get(timeout=secs)
            print(f"[TrueClicks Direct] {label}: {kind}")
            return kind, data
        except queue.Empty:
            print(f"[TrueClicks Direct] Timeout waiting for {label}")
            return "timeout", None

    try:
        # 1. Wait for endpoint URI
        kind, data = wait("endpoint", secs=8)
        if kind != "endpoint":
            return None

        # 2. Initialize
        post({
            "jsonrpc": "2.0",
            "method":  "initialize",
            "params":  {
                "protocolVersion": "2024-11-05",
                "capabilities":    {"tools": {}},
                "clientInfo":      {"name": "suncrest-dashboard", "version": "1.0"},
            },
            "id": 0,
        })
        kind, _ = wait("initialize result", secs=8)

        # 3. Initialized notification
        post({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 4. Call tool
        post({
            "jsonrpc": "2.0",
            "method":  "tools/call",
            "params":  {
                "name":      "google-ads-download-report",
                "arguments": {
                    "customerId":      int(customer_id),
                    "loginCustomerId": int(login_customer_id),
                    "query":           gaql_query,
                },
            },
            "id": 1,
        })

        # 5. Wait for tool result
        kind, data = wait("tool result", secs=timeout)
        if kind != "result":
            return None

        result = data.get("result", {})
        # MCP tool results have content array
        content = result.get("content", [])
        for item in content:
            if item.get("type") == "text":
                text = item["text"].strip()
                try:
                    return json.loads(text)
                except Exception:
                    # Try stripping markdown fences
                    for part in text.split("```"):
                        part = part.strip().lstrip("json").strip()
                        if part.startswith(("[", "{")):
                            try:
                                return json.loads(part)
                            except Exception:
                                pass
        return None

    except Exception as exc:
        print(f"[TrueClicks Direct] Error: {exc}")
        return None
