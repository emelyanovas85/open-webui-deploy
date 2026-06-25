#!/usr/bin/env python3
"""
init-openwebui.py -- runs once on startup via open-webui-init container.

MCP protocol (Streamable HTTP, stateful mode):
  1. POST /mcp  initialize            -> response contains Mcp-Session-Id header
  2. POST /mcp  notifications/initialized  (with Mcp-Session-Id)
  3. POST /mcp  tools/list            (with Mcp-Session-Id)
  4. POST /mcp  tools/call            (with Mcp-Session-Id)

NOTE: supergateway MUST be started with --stateful flag.
      Without it every POST is a new process and session is lost between requests.

MCP_SERVER_URLS format: url1::name1,url2::name2
Example:
  MCP_SERVER_URLS=http://10.1.5.97:8086/mcp::Java MCP,http://10.1.5.97:8083::GitLab MCP

MCP Tool Servers are registered in Open WebUI UI via POST /api/v1/configs/tool_servers
so that tools are visible in the chat interface (+/Tools button).
Open WebUI will probe registered servers on startup -- this is expected behaviour.
Registration happens AFTER a 10s delay to let the lifespan fully complete.
"""

import os
import sys
import time
import json
import sqlite3
import requests
from urllib.parse import urlparse

BASE_URL = os.environ.get("WEBUI_BASE_URL", "http://open-webui:8080").rstrip("/")
ADMIN_EMAIL = os.environ.get("WEBUI_ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.environ.get("WEBUI_ADMIN_PASSWORD", "")

MCP_SERVER_URLS_RAW = os.environ.get("MCP_SERVER_URLS", "")
MCP_SERVER_URL_LEGACY = os.environ.get("MCP_SERVER_URL", "")

DB_PATH = "/app/backend/data/webui.db"

PIPE_FUNCTION_ID = "cbr_models"
PIPE_FUNCTION_NAME = "CBR Models"


def detect_transport(url: str) -> dict:
    if url.endswith("/mcp"):
        return {"base_url": url[:-4], "path": "/mcp"}
    if url.endswith("/sse"):
        return {"base_url": url[:-4], "path": "/sse"}
    return {"base_url": url, "path": "/mcp"}


def parse_mcp_servers():
    servers = []
    if MCP_SERVER_URLS_RAW:
        for entry in MCP_SERVER_URLS_RAW.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split("::")
            raw_url = parts[0].strip()
            transport = detect_transport(raw_url)
            name = parts[1].strip() if len(parts) > 1 else raw_url.split("/")[2].replace(":", "_")
            desc = parts[2].strip() if len(parts) > 2 else name
            servers.append({
                "url": transport["base_url"],
                "path": transport["path"],
                "name": name,
                "description": desc,
            })
    elif MCP_SERVER_URL_LEGACY:
        transport = detect_transport(MCP_SERVER_URL_LEGACY)
        servers.append({
            "url": transport["base_url"],
            "path": transport["path"],
            "name": "mcp_server",
            "description": "MCP Tool Server",
        })
    return servers


def parse_mcp_server_urls(env_value):
    servers = []
    for entry in env_value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("::")
        raw_url = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else raw_url
        parsed = urlparse(raw_url)
        base_url = "%s://%s" % (parsed.scheme, parsed.netloc)
        path = parsed.path.rstrip("/") or "/mcp"
        servers.append({"url": base_url, "path": path, "name": name})
    return servers


_MCP_ENV = os.environ.get("MCP_SERVER_URLS", "")
if _MCP_ENV:
    _MCP_SERVERS_PARSED = parse_mcp_server_urls(_MCP_ENV)
    print("[CONFIG] MCP_SERVER_URLS from env: %d server(s)" % len(_MCP_SERVERS_PARSED))
    for s in _MCP_SERVERS_PARSED:
        print("         * %s  %s%s" % (s["name"], s["url"], s["path"]))
else:
    _MCP_SERVERS_PARSED = [
        {"url": "http://10.1.5.97:8086", "path": "/mcp", "name": "Java MCP"},
        {"url": "http://10.1.5.97:8083", "path": "/mcp", "name": "GitLab MCP"},
    ]
    print("[CONFIG] MCP_SERVER_URLS not set -- using defaults")

_MCP_SERVERS_JSON = json.dumps(_MCP_SERVERS_PARSED)

# IMPORTANT: inside _PIPE_FUNCTION_TEMPLATE we must NOT use f-strings that contain
# quotes or parentheses -- Open WebUI compiles the code via compile() and such
# constructs cause SyntaxError. Use % formatting or intermediate variables only.
_PIPE_FUNCTION_TEMPLATE = '''
"""
title: CBR Models
author: local
version: 4.8
description: Dynamic CBR models list + MCP tool calling via stateful supergateway session.
"""

import httpx
import ssl
import json
import uuid

UPSTREAM_BASE = "https://chat.ehd-zr.cbr.ru/openai"
API_KEY = "sk-09fd660cdc8640ac861fe85a16d2d2f1"

MCP_SERVERS = __MCP_SERVERS_JSON__

MODELS_CACHE = []
_mcp_tools_cache = None


def get_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def fetch_models():
    global MODELS_CACHE
    try:
        ssl_ctx = get_ssl_context()
        with httpx.Client(verify=ssl_ctx, timeout=30.0) as client:
            r = client.get(
                UPSTREAM_BASE + "/models",
                headers={"Authorization": "Bearer " + API_KEY},
            )
            r.raise_for_status()
            data = r.json().get("data", [])
            MODELS_CACHE = [{"id": m["id"], "name": m.get("name", m["id"])} for m in data]
    except Exception:
        if not MODELS_CACHE:
            MODELS_CACHE = [
                {"id": "Qwen/Qwen3.5-397B-A17B-GPTQ-Int4", "name": "Qwen3.5 397B (fallback)"},
            ]
    return MODELS_CACHE


def _parse_sse(text):
    """Extract first JSON object from SSE data: lines."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                try:
                    return json.loads(data)
                except Exception:
                    pass
    return None


def _mcp_post(url, session_id, payload):
    """
    Single POST to MCP endpoint.
    Returns (response_dict_or_None, session_id_str).
    session_id from initialize response header is returned for subsequent calls.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, read=20.0)) as client:
            resp = client.post(url, content=json.dumps(payload), headers=headers)
            # session id returned by server after initialize
            new_session = resp.headers.get("Mcp-Session-Id") or session_id
            if resp.status_code not in (200, 202):
                return None, new_session
            ct = resp.headers.get("content-type", "")
            body = resp.text.strip()
            if not body:
                return None, new_session
            if "text/event-stream" in ct:
                obj = _parse_sse(body)
            else:
                try:
                    obj = json.loads(body)
                except Exception:
                    obj = None
            return obj, new_session
    except Exception as e:
        srv = url
        print("[MCP] POST error %s: %s" % (srv, str(e)))
        return None, session_id


def _mcp_fetch_tools_stateful(server):
    """
    Fetch tools using stateful session (supergateway --stateful):
      1. POST initialize  -> get Mcp-Session-Id
      2. POST notifications/initialized  (with session id)
      3. POST tools/list  (with session id)
    """
    url = server["url"] + server["path"]
    srv_name = server["name"]

    # Step 1: initialize
    init_id = str(uuid.uuid4())
    resp, session_id = _mcp_post(url, None, {
        "jsonrpc": "2.0",
        "id": init_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "cbr-pipe", "version": "4.8"},
        },
    })
    if not resp or resp.get("id") != init_id:
        print("[MCP] initialize failed for %s" % srv_name)
        return []

    # Step 2: notifications/initialized (no id = notification, no response expected)
    _mcp_post(url, session_id, {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    })

    # Step 3: tools/list
    list_id = str(uuid.uuid4())
    resp2, _ = _mcp_post(url, session_id, {
        "jsonrpc": "2.0",
        "id": list_id,
        "method": "tools/list",
        "params": {},
    })
    if resp2 and resp2.get("id") == list_id:
        return resp2.get("result", {}).get("tools", [])
    return []


def _fetch_mcp_tools():
    global _mcp_tools_cache
    if _mcp_tools_cache is not None and len(_mcp_tools_cache) > 0:
        return _mcp_tools_cache
    tools_map = {}
    for srv in MCP_SERVERS:
        srv_name = srv["name"]
        try:
            tools = _mcp_fetch_tools_stateful(srv)
            for t in tools:
                tools_map[t["name"]] = {"server": srv, "schema": t}
            print("[MCP] %d tools from %s" % (len(tools), srv_name))
        except Exception as e:
            print("[MCP] error from %s: %s" % (srv_name, str(e)))
    _mcp_tools_cache = tools_map if tools_map else None
    return tools_map


def _call_mcp_tool(tool_name, tool_args):
    """
    Call a tool using a fresh stateful session:
      1. initialize -> session_id
      2. notifications/initialized
      3. tools/call
    """
    tools_map = _fetch_mcp_tools()
    entry = tools_map.get(tool_name) if tools_map else None
    if not entry:
        return json.dumps({"error": "Tool not found: " + tool_name})
    srv = entry["server"]
    url = srv["url"] + srv["path"]
    srv_name = srv["name"]

    # Step 1: initialize
    init_id = str(uuid.uuid4())
    resp, session_id = _mcp_post(url, None, {
        "jsonrpc": "2.0",
        "id": init_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "cbr-pipe", "version": "4.8"},
        },
    })
    if not resp or resp.get("id") != init_id:
        return json.dumps({"error": "initialize failed for " + srv_name})

    # Step 2: notifications/initialized
    _mcp_post(url, session_id, {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    })

    # Step 3: tools/call
    call_id = str(uuid.uuid4())
    resp2, _ = _mcp_post(url, session_id, {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": tool_args},
    })
    if resp2 and resp2.get("id") == call_id:
        result = resp2.get("result", {})
        content = result.get("content", [])
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text"]
            return "\\n".join(texts) if texts else json.dumps(result)
        return json.dumps(result)
    return json.dumps({"error": "No response for tool: " + tool_name})


def _tools_for_llm():
    tools_map = _fetch_mcp_tools()
    if not tools_map:
        return []
    result = []
    for name, entry in tools_map.items():
        schema = entry["schema"]
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": schema.get("description", ""),
                "parameters": schema.get("inputSchema",
                    {"type": "object", "properties": {}}),
            },
        })
    return result


class Pipe:
    def __init__(self):
        self.id = "cbr_models"
        self.name = "CBR Models"
        self.type = "manifold"

    def pipes(self):
        models = fetch_models()
        return [{"id": m["id"], "name": m["name"]} for m in models]

    def _resolve_model_id(self, body):
        raw = body.get("model", "")
        prefix = self.id + "."
        if raw.startswith(prefix):
            return raw[len(prefix):]
        return raw

    def _llm_call(self, model, messages, tools, stream=False, extra=None):
        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if extra:
            for key in ("temperature", "max_tokens", "top_p",
                        "presence_penalty", "frequency_penalty"):
                if key in extra:
                    payload[key] = extra[key]
        ssl_ctx = get_ssl_context()
        headers = {
            "Authorization": "Bearer " + API_KEY,
            "Content-Type": "application/json",
        }
        if stream:
            headers["Accept"] = "text/event-stream"
            return self._stream_raw(payload, headers, ssl_ctx)
        with httpx.Client(verify=ssl_ctx, timeout=120.0) as client:
            r = client.post(
                UPSTREAM_BASE + "/chat/completions",
                headers=headers,
                json=payload,
            )
            r.raise_for_status()
            return r.json()

    def _stream_raw(self, payload, headers, ssl_ctx):
        with httpx.Client(verify=ssl_ctx, timeout=120.0) as client:
            with client.stream(
                "POST",
                UPSTREAM_BASE + "/chat/completions",
                headers=headers,
                json=payload,
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="ignore")
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content

    def pipe(self, body):
        model = self._resolve_model_id(body)
        messages = list(body.get("messages", []))
        stream = body.get("stream", False)
        extra = {k: body[k] for k in
                 ("temperature", "max_tokens", "top_p",
                  "presence_penalty", "frequency_penalty") if k in body}

        tools = body.get("tools") or _tools_for_llm()

        MAX_ITERATIONS = 10
        for iteration in range(MAX_ITERATIONS):
            is_last = (iteration == MAX_ITERATIONS - 1)
            use_stream = stream and (is_last or not tools)

            resp = self._llm_call(
                model,
                messages,
                tools if not is_last else [],
                stream=use_stream,
                extra=extra,
            )

            if use_stream:
                return resp

            choice = resp.get("choices", [{}])[0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "stop")
            tool_calls = message.get("tool_calls") or []

            if not tool_calls or finish_reason == "stop":
                content = message.get("content", "")
                if stream:
                    def _single(text):
                        yield text
                    return _single(content)
                return content

            messages.append(message)

            for tc in tool_calls:
                fn = tc.get("function", {})
                t_name = fn.get("name", "")
                try:
                    t_args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    t_args = {}

                t_result = _call_mcp_tool(t_name, t_args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", str(uuid.uuid4())),
                    "content": t_result,
                })

        return "[Tool calling iteration limit reached]"
'''

PIPE_FUNCTION_CODE = _PIPE_FUNCTION_TEMPLATE.replace("__MCP_SERVERS_JSON__", _MCP_SERVERS_JSON)


def wait_for_webui(max_retries=30, delay=5):
    for i in range(max_retries):
        try:
            r = requests.get("%s/health" % BASE_URL, timeout=5)
            if r.status_code == 200:
                print("[OK] Open WebUI is up after %ds" % (i * delay))
                return True
        except Exception:
            pass
        print("[..] Waiting for Open WebUI... (%d/%d)" % (i + 1, max_retries))
        time.sleep(delay)
    print("[ERROR] Open WebUI did not start in time")
    return False


def get_token():
    r = requests.post(
        "%s/api/v1/auths/signin" % BASE_URL,
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    if r.status_code == 200:
        token = r.json().get("token")
        print("[OK] Signed in as %s" % ADMIN_EMAIL)
        return token

    print("[..] Login failed (%d), trying signup..." % r.status_code)
    r = requests.post(
        "%s/api/v1/auths/signup" % BASE_URL,
        json={
            "name": os.environ.get("WEBUI_ADMIN_NAME", "Admin"),
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
        },
        timeout=10,
    )
    if r.status_code in (200, 201):
        token = r.json().get("token")
        print("[OK] Admin account created: %s" % ADMIN_EMAIL)
        return token

    print("[ERROR] Cannot get token: %d %s" % (r.status_code, r.text))
    return None


def upsert_pipe_function(token):
    headers = {"Authorization": "Bearer %s" % token, "Content-Type": "application/json"}

    for old_id in ("qwen_cbr",):
        r = requests.get("%s/api/v1/functions/id/%s" % (BASE_URL, old_id), headers=headers, timeout=10)
        if r.status_code == 200:
            requests.delete("%s/api/v1/functions/id/%s" % (BASE_URL, old_id), headers=headers, timeout=10)
            print("[OK] Removed old pipe function: %s" % old_id)

    payload = {
        "id": PIPE_FUNCTION_ID,
        "name": PIPE_FUNCTION_NAME,
        "content": PIPE_FUNCTION_CODE,
        "is_active": True,
        "meta": {
            "description": "Dynamic CBR models via SSL-compatible Pipe + MCP tool calling",
            "manifest": {"title": PIPE_FUNCTION_NAME},
        },
    }

    r = requests.get("%s/api/v1/functions/id/%s" % (BASE_URL, PIPE_FUNCTION_ID), headers=headers, timeout=10)
    if r.status_code == 200:
        r = requests.post(
            "%s/api/v1/functions/id/%s/update" % (BASE_URL, PIPE_FUNCTION_ID),
            headers=headers, json=payload, timeout=10,
        )
        print("[OK] Pipe function updated: %d" % r.status_code)
    else:
        r = requests.post(
            "%s/api/v1/functions/create" % BASE_URL,
            headers=headers, json=payload, timeout=10,
        )
        print("[OK] Pipe function created: %d" % r.status_code)

    if r.status_code not in (200, 201):
        print("[WARN] Pipe function response: %s" % r.text[:300])
        return

    tr = requests.post(
        "%s/api/v1/functions/id/%s/toggle" % (BASE_URL, PIPE_FUNCTION_ID),
        headers=headers, timeout=10,
    )
    toggled = tr.json() if tr.status_code == 200 else {}
    is_active = toggled.get("is_active")
    if is_active is False:
        tr = requests.post(
            "%s/api/v1/functions/id/%s/toggle" % (BASE_URL, PIPE_FUNCTION_ID),
            headers=headers, timeout=10,
        )
        toggled = tr.json() if tr.status_code == 200 else {}
        is_active = toggled.get("is_active")
    print("[OK] Pipe function active: %s (toggle status: %d)" % (is_active, tr.status_code))


def make_stub_info(url, name="mcp"):
    return {"id": url, "name": name, "version": "0.1.0"}


def sync_mcp_tool_servers(token):
    """Register MCP servers in Open WebUI UI via /api/v1/configs/tool_servers.

    This makes tools visible in the chat interface (+ button -> Tools).
    Called with a 10s delay after upsert_pipe_function to let the Open WebUI
    lifespan fully complete before we register servers (avoids WebSocket crash).
    """
    servers = parse_mcp_servers()
    if not servers:
        print("[SKIP] No MCP servers configured (MCP_SERVER_URLS not set)")
        return

    headers = {"Authorization": "Bearer %s" % token, "Content-Type": "application/json"}

    new_connections = []
    for srv in servers:
        conn = {
            "url": srv["url"],
            "path": srv["path"],
            "type": "mcp",
            "auth_type": "none",
            "key": "",
            "headers": None,
            "config": {"enable": True},
            "info": make_stub_info(srv["url"], srv["name"]),
        }
        new_connections.append(conn)
        print("[INFO] MCP server: %s -> %s (path: %s)" % (srv["name"], srv["url"], srv["path"]))

    r = requests.post(
        "%s/api/v1/configs/tool_servers" % BASE_URL,
        headers=headers,
        json={"TOOL_SERVER_CONNECTIONS": new_connections},
        timeout=15,
    )
    if r.status_code in (200, 201):
        for srv in servers:
            print("[OK] MCP server registered: %s -> %s" % (srv["name"], srv["url"]))
    else:
        print("[WARN] POST /configs/tool_servers %d: %s" % (r.status_code, r.text[:300]))


def patch_db(db_path):
    """Disable built-in OpenAI connections in DB config.

    NOTE: tool_server.connections are NOT cleared here -- they are managed
    by sync_mcp_tool_servers() via the API. Clearing them would wipe the
    registered MCP servers and tools would disappear from the UI.
    """
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT id, data FROM config ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            print("[WARN] DB: config table is empty")
            conn.close()
            return
        config_id, raw = row
        data = json.loads(raw)
        patched = False

        openai_connections = data.get("openai", {}).get("connections", [])
        disabled_count = 0
        for c in openai_connections:
            if c.get("enabled", True):
                c["enabled"] = False
                disabled_count += 1
                patched = True
        if disabled_count:
            print("[PATCH] Disabled %d OpenAI connection(s) in DB" % disabled_count)
        else:
            print("[OK] DB: no active OpenAI connections found")

        if patched:
            cur.execute(
                "UPDATE config SET data = ? WHERE id = ?",
                (json.dumps(data), config_id)
            )
            conn.commit()
            print("[OK] DB patched successfully")
        else:
            print("[OK] DB check passed - nothing to patch")

        conn.close()
    except Exception as e:
        print("[ERROR] DB patch failed: %s" % e)


def main():
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        print("[ERROR] WEBUI_ADMIN_EMAIL and WEBUI_ADMIN_PASSWORD must be set")
        sys.exit(1)

    if not wait_for_webui():
        sys.exit(1)

    token = get_token()
    if not token:
        sys.exit(1)

    upsert_pipe_function(token)

    # Extra delay to let Open WebUI lifespan fully complete before registering
    # MCP servers -- prevents the /sse 404 -> lifespan exception -> WebSocket crash.
    print("[..] Waiting 10s for Open WebUI lifespan to settle...")
    time.sleep(10)

    sync_mcp_tool_servers(token)

    time.sleep(5)
    patch_db(DB_PATH)

    print("[DONE] Init completed successfully")


if __name__ == "__main__":
    main()
