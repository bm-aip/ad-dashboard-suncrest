"""
Direct TrueClicks MCP SSE client.
Bypasses Anthropic API — connects directly to TrueClicks MCP server.

TrueClicks SSE protocol:
  GET  {mcp_url}  → SSE stream
  First event:    event: endpoint
                  data: /messages?sessionId={id}   ← relative path, NOT JSON
  POST {base_url}/messages?sessionId={id} → JSON-RPC messages
  Responses come back via the same SSE stream.
"""
import json
import queue
import threading
import requests
from urllib.parse import urlparse


def _base_url(mcp_url):
    """Extract https://host from the full MCP URL."""
    p = urlparse(mcp_url)
    return f"{p.scheme}://{p.netloc}"


def call_trueclicks_gaql(mcp_url, customer_id, login_customer_id, gaql_query, timeout=30):
    """
    Call TrueClicks MCP server directly via SSE transport.
    Returns list of raw GAQL row dicts, or None on failure.
    """
    result_q  = queue.Queue()
    post_url  = [None]
    base      = _base_url(mcp_url)

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
                        data_str = line[5:].strip()

                        # ── Endpoint event: data is a relative path string ──
                        if event_type == "endpoint":
                            if data_str.startswith("/"):
                                uri = base + data_str
                            elif data_str.startswith("http"):
                                uri = data_str
                            else:
                                # Might be JSON with uri key
                                try:
                                    d = json.loads(data_str)
                                    uri = d.get("uri") or d.get("url", "")
                                    if not uri:
                                        continue
                                except Exception:
                                    continue
                            post_url[0] = uri
                            result_q.put(("endpoint", uri))
                            continue

                        # ── JSON-RPC response ──
                        try:
                            data = json.loads(data_str)
                        except Exception:
                            continue

                        if isinstance(data, dict) and "jsonrpc" in data:
                            if "error" in data:
                                result_q.put(("error", data["error"]))
                            elif "result" in data:
                                result_q.put(("result", data))

        except Exception as exc:
            result_q.put(("sse_error", str(exc)))

    # Start SSE reader
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
            print(f"[TrueClicks Direct] Did not get endpoint, got: {kind}")
            return None
        print(f"[TrueClicks Direct] POST URL: {post_url[0]}")

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
        kind, _ = wait("initialize", secs=8)
        if kind not in ("result", "timeout"):
            print(f"[TrueClicks Direct] Unexpected init response: {kind}")

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
            print(f"[TrueClicks Direct] Tool call failed: {kind}, {data}")
            return None

        result = data.get("result", {})

        # Extract text content from MCP tool result
        content = result.get("content", [])
        for item in content:
            if item.get("type") == "text":
                text = item["text"].strip()
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        return parsed
                    # Some tools return {results: [...]}
                    if isinstance(parsed, dict):
                        for key in ("results", "rows", "data", "campaigns"):
                            if key in parsed:
                                return parsed[key]
                    return parsed
                except Exception:
                    # Try stripping markdown
                    for part in text.split("```"):
                        part = part.strip().lstrip("json").strip()
                        if part.startswith("[") or part.startswith("{"):
                            try:
                                return json.loads(part)
                            except Exception:
                                pass

        # If content is empty, result itself might be the data
        if isinstance(result, list):
            return result

        print(f"[TrueClicks Direct] Could not extract rows from result: {str(result)[:300]}")
        return None

    except Exception as exc:
        print(f"[TrueClicks Direct] Error: {exc}")
        return None
