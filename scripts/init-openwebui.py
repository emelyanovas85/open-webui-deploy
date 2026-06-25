#!/usr/bin/env python3
"""
init-openwebui.py -- runs once on startup via open-webui-init container.

KEY: supergateway works in STATELESS mode -- each POST creates a new stdio process.
So initialize + notifications/initialized + tools/list MUST go in ONE batch POST (JSON array).

MCP_SERVER_URLS format: url1::name1,url2::name2
Example:
  MCP_SERVER_URLS=http://10.1.5.97:8086/mcp::Java MCP,http://10.1.5.97:8083::GitLab MCP
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

DB_PATH = "/app/backend/data/webui.db"

PIPE_FUNCTION_ID = "cbr_models"
PIPE_FUNCTION_NAME = "CBR Models"


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
# quotes or parentheses, because Open WebUI compiles the function code via compile()
# and such constructs cause SyntaxError. Use % formatting or intermediate variables.
_PIPE_FUNCTION_TEMPLATE = '''
"""
title: CBR Models
author: local
version: 4.7
description: Dynamic CBR models list + MCP tool calling. Stateless MCP via batch requests.
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


def _parse_sse_batch(text):
    results = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                try:
                    obj = json.loads(data)
                    if isinstance(obj, list):
                        results.extend(obj)
                    else:
                        results.append(obj)
                except Exception:
                    pass
    return results


def _mcp_batch(server, requests_list):
    """
    Send a JSON-RPC batch (list) in a single POST.
    supergateway (stateless) processes the whole batch in one stdio process.
    Returns list of response objects.
    """
    url = server["url"] + server["path"]
    srv_name = server["name"]
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, read=20.0)) as client:
            resp = client.post(url, content=json.dumps(requests_list), headers=headers)
            if resp.status_code not in (200, 202):
                print("[MCP] batch HTTP %d from %s" % (resp.status_code, srv_name))
                return []
            ct = resp.headers.get("content-type", "")
            body = resp.text.strip()
            if not body:
                return []
            if "text/event-stream" in ct:
                return _parse_sse_batch(body)
            try:
                obj = json.loads(body)
                return obj if isinstance(obj, list) else [obj]
            except Exception:
                return []
    except Exception as e:
        print("[MCP] batch error from %s: %s" % (srv_name, str(e)))
        return []


def _mcp_fetch_tools_stateless(server):
    """
    Fetch tools in ONE batch: [initialize, notifications/initialized, tools/list]
    Required because supergateway is stateless -- each POST is a new process.
    """
    init_id = str(uuid.uuid4())
    list_id = str(uuid.uuid4())
    batch = [
        {
            "jsonrpc": "2.0",
            "id": init_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cbr-pipe", "version": "4.7"},
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        },
        {
            "jsonrpc": "2.0",
            "id": list_id,
            "method": "tools/list",
            "params": {},
        },
    ]
    responses = _mcp_batch(server, batch)
    for resp in responses:
        if isinstance(resp, dict) and resp.get("id") == list_id:
            return resp.get("result", {}).get("tools", [])
    return []


def _fetch_mcp_tools():
    global _mcp_tools_cache
    if _mcp_tools_cache is not None and len(_mcp_tools_cache) > 0:
        return _mcp_tools_cache
    tools_map = {}
    for srv in MCP_SERVERS:
        srv_name = srv["name"]
        try:
            tools = _mcp_fetch_tools_stateless(srv)
            for t in tools:
                tools_map[t["name"]] = {"server": srv, "schema": t}
            print("[MCP] %d tools from %s" % (len(tools), srv_name))
        except Exception as e:
            print("[MCP] error from %s: %s" % (srv_name, str(e)))
    _mcp_tools_cache = tools_map if tools_map else None
    return tools_map


def _call_mcp_tool(tool_name, tool_args):
    tools_map = _fetch_mcp_tools()
    entry = tools_map.get(tool_name) if tools_map else None
    if not entry:
        return json.dumps({"error": "Tool not found: " + tool_name})
    srv = entry["server"]
    init_id = str(uuid.uuid4())
    call_id = str(uuid.uuid4())
    batch = [
        {
            "jsonrpc": "2.0",
            "id": init_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cbr-pipe", "version": "4.7"},
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        },
        {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": tool_args},
        },
    ]
    responses = _mcp_batch(srv, batch)
    for resp in responses:
        if isinstance(resp, dict) and resp.get("id") == call_id:
            result = resp.get("result", {})
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


def patch_db(db_path):
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

        tool_servers = data.get("tool_server", {}).get("connections", [])
        if tool_servers:
            data.setdefault("tool_server", {})["connections"] = []
            print("[PATCH] Cleared %d tool server connection(s) from DB" % len(tool_servers))
            patched = True
        else:
            print("[OK] DB: no tool server connections to clear")

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

    time.sleep(5)
    patch_db(DB_PATH)

    print("[DONE] Init completed successfully")


if __name__ == "__main__":
    main()
