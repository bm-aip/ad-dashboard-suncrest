"""
Direct TrueClicks MCP SSE client — verified protocol from debug session.

Protocol (confirmed):
  1. GET {mcp_url}
     → SSE stream, first event: event: endpoint / data: /messages?sessionId={id}
  2. POST {base}/messages?sessionId={id}  ← JSON-RPC messages
     → HTTP 202 Accepted (empty body)
  3. All responses come back via the SSE stream as JSON-RPC events

TrueClicks tool result structure (confirmed):
  SSE event data = {
    "jsonrpc": "2.0", "id": 1,
    "result": {
      "content": [
        {
          "type": "text",
          "text": '{"userLogin":null,"notification":{...},"result":{"columns":[...],"data":[[...],...]}}' 
        }
      ]
    }
  }

Column names: dotted paths with camelCase metrics e.g. "metrics.costMicros"
Data: array of arrays (strings), matching columns order
"""

import json
import queue
import re
import threading
import requests
from urllib.parse import urlparse


def _base_url(mcp_url):
    p = urlparse(mcp_url)
    return f"{p.scheme}://{p.netloc}"


def _camel_to_snake(name):
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


def _parse_trueclicks_result(mcp_result):
    """
    Parse TrueClicks MCP result into list of nested GAQL-style dicts.

    mcp_result = {"content": [{"type": "text", "text": "...json..."}]}

    Inner JSON = {
      "userLogin": null, "notification": {...},
      "result": {
        "columns": ["campaign.name", "metrics.costMicros", ...],
        "data": [["CampaignA", "12345678", "3"], ...]   ← array of string arrays
      }
    }
    """
    # Extract text from MCP content array
    if not isinstance(mcp_result, dict):
        print(f"[TrueClicks Parse] Unexpected mcp_result type: {type(mcp_result)}")
        return None

    text = None
    for item in mcp_result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "").strip()
            break

    if not text:
        print(f"[TrueClicks Parse] No text content. Keys: {list(mcp_result.keys())}")
        return None

    # Parse inner JSON
    try:
        tc = json.loads(text)
    except Exception as exc:
        print(f"[TrueClicks Parse] JSON parse error: {exc}. Text[:200]: {text[:200]}")
        return None

    # Navigate to columns + data
    # tc = {"userLogin": null, "notification": {...}, "result": {"columns": [...], "data": [...]}}
    inner = tc.get("result", tc)
    columns = inner.get("columns", [])
    rows    = inner.get("data",    inner.get("rows", []))

    if not columns:
        print(f"[TrueClicks Parse] No columns. Inner keys: {list(inner.keys())}")
        return None

    print(f"[TrueClicks Parse] {len(rows)} rows, columns: {columns}")

    result_list = []
    for row in rows:
        if isinstance(row, list):
            row_dict = dict(zip(columns, row))
        elif isinstance(row, dict):
            row_dict = row
        else:
            continue

        # Build nested dict from dotted paths, store both camelCase and snake_case
        nested = {}
        for col, val in row_dict.items():
            parts = col.split(".")
            d = nested
            for part in parts[:-1]:
                if part not in d or not isinstance(d[part], dict):
                    d[part] = {}
                d = d[part]
            leaf  = parts[-1]
            snake = _camel_to_snake(leaf)
            d[leaf] = val
            if snake != leaf:
                d[snake] = val

        result_list.append(nested)

    return result_list


def call_trueclicks_gaql(mcp_url, customer_id, login_customer_id, gaql_query, timeout=30):
    """
    Call TrueClicks MCP server directly.
    Keeps SSE connection open in background thread; sends JSON-RPC via HTTP POST.
    Returns list of nested GAQL-style dicts, or None on failure.
    """
    result_q = queue.Queue()
    post_url  = [None]
    base      = _base_url(mcp_url)

    # ── SSE reader thread ─────────────────────────────────────────────────────
    def sse_reader():
        try:
            with requests.get(
                mcp_url,
                stream=True,
                headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
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

                        if event_type == "endpoint":
                            uri = (base + data_str) if data_str.startswith("/") else data_str
                            post_url[0] = uri
                            result_q.put(("endpoint", uri))
                            continue

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
            result_q.put(("sse_error", str(exc)[:200]))

    t = threading.Thread(target=sse_reader, daemon=True)
    t.start()

    # ── Helper: wait for queue item ────────────────────────────────────────────
    def wait(label, secs):
        try:
            kind, val = result_q.get(timeout=secs)
            print(f"[TrueClicks] {label}: {kind}")
            return kind, val
        except queue.Empty:
            print(f"[TrueClicks] Timeout: {label}")
            return "timeout", None

    # ── Helper: POST JSON-RPC message ──────────────────────────────────────────
    def post(payload):
        try:
            requests.post(
                post_url[0],
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
        except Exception as exc:
            print(f"[TrueClicks] POST error: {exc}")

    try:
        # 1. Get endpoint URI
        kind, val = wait("endpoint", secs=8)
        if kind != "endpoint":
            print(f"[TrueClicks] No endpoint URI, got: {kind} / {val}")
            return None
        print(f"[TrueClicks] Endpoint: {post_url[0]}")

        # 2. Initialize
        post({
            "jsonrpc": "2.0", "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "suncrest-dashboard", "version": "1.0"},
            },
            "id": 0,
        })
        wait("initialize", secs=8)  # don't abort on timeout

        # 3. Initialized notification
        post({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 4. Tool call
        post({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {
                "name": "google-ads-download-report",
                "arguments": {
                    "customerId":      int(customer_id),
                    "loginCustomerId": int(login_customer_id),
                    "query":           gaql_query,
                },
            },
            "id": 1,
        })

        # 5. Wait for tool result (long wait — queries can take 10-15s)
        kind, data = wait("tool result", secs=timeout)
        if kind != "result":
            print(f"[TrueClicks] Tool call failed: {kind} / {data}")
            return None

        # 6. Parse result
        mcp_result = data.get("result")
        rows = _parse_trueclicks_result(mcp_result)
        return rows

    except Exception as exc:
        print(f"[TrueClicks] Error: {exc}")
        return None
