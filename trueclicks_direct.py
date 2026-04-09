"""
Direct TrueClicks MCP SSE client.
Bypasses Anthropic API — connects directly to TrueClicks MCP server.

TrueClicks SSE protocol:
  GET  {mcp_url}               → SSE stream
  First event:  event: endpoint
                data: /messages?sessionId={id}   ← relative path string
  POST {base}/messages?sessionId={id} → JSON-RPC messages
  Responses come back via the same SSE stream.

TrueClicks tool result format (NOT standard MCP content format):
  {
    "userLogin": null,
    "notification": {"errors": [], "warnings": [], "infos": []},
    "result": {
      "columns": ["campaign.name", "metrics.costMicros", ...],
      "rows": [...]
    }
  }

Column names use dotted paths with camelCase for metrics:
  campaign.name, metrics.costMicros, metrics.conversions, etc.
"""

import json
import queue
import threading
import re
import requests
from urllib.parse import urlparse


def _base_url(mcp_url):
    p = urlparse(mcp_url)
    return f"{p.scheme}://{p.netloc}"


def _camel_to_snake(name):
    """Convert camelCase to snake_case. e.g. costMicros → cost_micros"""
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


def _parse_trueclicks_rows(tc_result):
    """
    Parse TrueClicks result format into list of GAQL-style nested dicts.

    TrueClicks returns:
      {"userLogin": null, "notification": {...}, "result": {"columns": [...], "rows": [...]}}

    We convert to:
      [{"campaign": {"name": "..."}, "metrics": {"cost_micros": 123, "costMicros": 123, ...}}, ...]
    """
    if not isinstance(tc_result, dict):
        # Already a list or unexpected format
        if isinstance(tc_result, list):
            return tc_result
        return None

    # Navigate to inner result
    inner = tc_result.get("result", tc_result)
    if isinstance(inner, dict) and "columns" not in inner and "result" in inner:
        inner = inner["result"]

    columns = inner.get("columns", []) if isinstance(inner, dict) else []
    rows    = inner.get("rows",    []) if isinstance(inner, dict) else []

    if not columns:
        print(f"[TrueClicks Parse] No columns found. Keys: {list(tc_result.keys())}")
        return None

    print(f"[TrueClicks Parse] {len(rows)} rows, columns: {columns}")

    result_list = []
    for row in rows:
        # Rows can be list (parallel to columns) or dict (column → value)
        if isinstance(row, list):
            row_dict = dict(zip(columns, row))
        elif isinstance(row, dict):
            # Might be flat {"campaign.name": v} or already nested {"campaign": {"name": v}}
            row_dict = row
        else:
            continue

        # Build nested dict from dotted column paths
        nested = {}
        for col, val in row_dict.items():
            parts = col.split(".")
            d = nested
            for part in parts[:-1]:
                if part not in d:
                    d[part] = {}
                elif not isinstance(d[part], dict):
                    d[part] = {}
                d = d[part]

            leaf_camel = parts[-1]
            leaf_snake = _camel_to_snake(leaf_camel)

            # Store under both camelCase and snake_case so callers can use either
            d[leaf_camel] = val
            if leaf_snake != leaf_camel:
                d[leaf_snake] = val

        result_list.append(nested)

    return result_list


def call_trueclicks_gaql(mcp_url, customer_id, login_customer_id, gaql_query, timeout=30):
    """
    Call TrueClicks MCP server directly via SSE transport.
    Returns list of nested GAQL-style row dicts, or None on failure.
    """
    result_q = queue.Queue()
    post_url = [None]
    base     = _base_url(mcp_url)

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

                        # Endpoint event — plain relative path string
                        if event_type == "endpoint":
                            if data_str.startswith("/"):
                                uri = base + data_str
                            elif data_str.startswith("http"):
                                uri = data_str
                            else:
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

                        # JSON-RPC response
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
            print(f"[TrueClicks Direct] Timeout: {label}")
            return "timeout", None

    try:
        # 1. Endpoint URI
        kind, data = wait("endpoint", secs=8)
        if kind != "endpoint":
            return None
        print(f"[TrueClicks Direct] POST URL: {post_url[0]}")

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
        wait("initialize", secs=8)

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

        # 5. Tool result
        kind, data = wait("tool result", secs=timeout)
        if kind != "result":
            print(f"[TrueClicks Direct] Tool failed: {kind} / {data}")
            return None

        tc_result = data.get("result")
        print(f"[TrueClicks Direct] Result keys: {list(tc_result.keys()) if isinstance(tc_result, dict) else type(tc_result)}")

        rows = _parse_trueclicks_rows(tc_result)
        print(f"[TrueClicks Direct] Parsed {len(rows) if rows else 0} rows")
        return rows

    except Exception as exc:
        print(f"[TrueClicks Direct] Error: {exc}")
        return None
