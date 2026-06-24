#!/usr/bin/env python3
"""
init-openwebui.py — запускается один раз при старте через open-webui-init контейнер.

Что делает:
1. Ждёт пока Open WebUI поднимется
2. Получает JWT-токен администратора
3. Создаёт/обновляет Pipe Function + активирует её
4. Полностью заменяет MCP Tool Servers (без дублей) согласно .env — ТОЛЬКО через API
5. Патчит БД: отключает OpenAI connections (MCP не трогает)
"""

import os
import sys
import time
import json
import sqlite3
import requests

BASE_URL = os.environ.get("WEBUI_BASE_URL", "http://open-webui:8080").rstrip("/")
ADMIN_EMAIL = os.environ.get("WEBUI_ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.environ.get("WEBUI_ADMIN_PASSWORD", "")

MCP_SERVER_URLS_RAW = os.environ.get("MCP_SERVER_URLS", "")
MCP_SERVER_URL_LEGACY = os.environ.get("MCP_SERVER_URL", "")

DB_PATH = "/app/backend/data/webui.db"


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


PIPE_FUNCTION_ID = "cbr_models"
PIPE_FUNCTION_NAME = "CBR Models"

PIPE_FUNCTION_CODE = '''
"""
title: CBR Models
author: local
version: 4.0
description: Динамический список моделей с chat.ehd-zr.cbr.ru + полный MCP tool calling loop.
"""

import httpx
import ssl
import json
import uuid

UPSTREAM_BASE = "https://chat.ehd-zr.cbr.ru/openai"
API_KEY = "sk-09fd660cdc8640ac861fe85a16d2d2f1"

# MCP серверы — Pipe сам ходит на них напрямую
MCP_SERVERS = [
    {"url": "http://10.1.5.97:8086", "path": "/mcp", "name": "Java MCP"},
    {"url": "http://10.1.5.97:8083", "path": "/mcp", "name": "GitLab MCP"},
]

MODELS_CACHE = []
_mcp_tools_cache = None  # кэш инструментов: {"tool_name": {"server": ..., "schema": ...}}


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
                f"{UPSTREAM_BASE}/models",
                headers={"Authorization": f"Bearer {API_KEY}"},
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


def _mcp_request(server: dict, method: str, params: dict = None) -> dict:
    """Отправляет JSON-RPC запрос на MCP сервер (Streamable HTTP)."""
    url = server["url"] + server["path"]
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params or {},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=15.0)
        r.raise_for_status()
        # Streamable HTTP может вернуть SSE или plain JSON
        ct = r.headers.get("content-type", "")
        if "text/event-stream" in ct:
            # Парсим первый data: ...
            for line in r.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data and data != "[DONE]":
                        return json.loads(data)
            return {}
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def _fetch_mcp_tools() -> dict:
    """Получает все инструменты со всех MCP серверов. Возвращает {tool_name: {server, schema}}."""
    global _mcp_tools_cache
    if _mcp_tools_cache is not None:
        return _mcp_tools_cache

    tools_map = {}
    for srv in MCP_SERVERS:
        # initialize
        _mcp_request(srv, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "cbr-pipe", "version": "4.0"},
        })
        # tools/list
        resp = _mcp_request(srv, "tools/list")
        tools = resp.get("result", {}).get("tools", [])
        for t in tools:
            tools_map[t["name"]] = {"server": srv, "schema": t}

    _mcp_tools_cache = tools_map
    return tools_map


def _call_mcp_tool(tool_name: str, tool_args: dict) -> str:
    """Вызывает инструмент на соответствующем MCP сервере."""
    tools_map = _fetch_mcp_tools()
    entry = tools_map.get(tool_name)
    if not entry:
        return json.dumps({"error": f"Tool \'{tool_name}\' not found in any MCP server"})

    srv = entry["server"]
    resp = _mcp_request(srv, "tools/call", {"name": tool_name, "arguments": tool_args})
    result = resp.get("result", {})
    content = result.get("content", [])
    if isinstance(content, list):
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(texts) if texts else json.dumps(result)
    return json.dumps(result)


def _tools_for_llm() -> list:
    """Возвращает список инструментов в формате OpenAI function calling."""
    tools_map = _fetch_mcp_tools()
    result = []
    for name, entry in tools_map.items():
        schema = entry["schema"]
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": schema.get("description", ""),
                "parameters": schema.get("inputSchema", {"type": "object", "properties": {}}),
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

    def _resolve_model_id(self, body: dict) -> str:
        raw = body.get("model", "")
        prefix = f"{self.id}."
        if raw.startswith(prefix):
            return raw[len(prefix):]
        return raw

    def _llm_call(self, model: str, messages: list, tools: list, stream: bool = False, extra: dict = None):
        """Один вызов LLM. Возвращает полный ответ (dict) или генератор чанков."""
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
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
        if stream:
            headers["Accept"] = "text/event-stream"

        if stream:
            return self._stream_raw(payload, headers, ssl_ctx)
        else:
            with httpx.Client(verify=ssl_ctx, timeout=120.0) as client:
                r = client.post(f"{UPSTREAM_BASE}/chat/completions", headers=headers, json=payload)
                r.raise_for_status()
                return r.json()

    def _stream_raw(self, payload, headers, ssl_ctx):
        """Генератор стримингового ответа — только текстовые чанки."""
        with httpx.Client(verify=ssl_ctx, timeout=120.0) as client:
            with client.stream("POST", f"{UPSTREAM_BASE}/chat/completions",
                               headers=headers, json=payload) as r:
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

    def pipe(self, body: dict):
        model = self._resolve_model_id(body)
        messages = list(body.get("messages", []))
        stream = body.get("stream", False)
        extra = {k: body[k] for k in ("temperature", "max_tokens", "top_p",
                                       "presence_penalty", "frequency_penalty") if k in body}

        # Если в body уже есть tools от Open WebUI — берём их, иначе получаем сами
        tools = body.get("tools") or _tools_for_llm()

        # Agentic loop: вызываем LLM → если tool_calls → выполняем → добавляем results → снова LLM
        MAX_ITERATIONS = 10
        for iteration in range(MAX_ITERATIONS):
            # На последней итерации или если нет инструментов — стримим финальный ответ
            is_last = (iteration == MAX_ITERATIONS - 1)
            use_stream = stream and (is_last or not tools)

            resp = self._llm_call(model, messages, tools if not is_last else [], stream=use_stream, extra=extra)

            if use_stream:
                return resp  # генератор — Open WebUI сам стримит

            choice = resp.get("choices", [{}])[0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "stop")
            tool_calls = message.get("tool_calls") or []

            if not tool_calls or finish_reason == "stop":
                # Финальный текстовый ответ
                content = message.get("content", "")
                if stream:
                    # Имитируем стриминг одним чанком
                    def _single_chunk(text):
                        yield text
                    return _single_chunk(content)
                return content

            # Добавляем ответ модели с tool_calls в историю
            messages.append(message)

            # Вызываем каждый инструмент и добавляем результаты
            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                try:
                    tool_args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    tool_args = {}

                tool_result = _call_mcp_tool(tool_name, tool_args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", str(uuid.uuid4())),
                    "content": tool_result,
                })

        # Fallback — если вышли из цикла без финального ответа
        return "[Превышен лимит итераций tool calling]"
'''


def wait_for_webui(max_retries=30, delay=5):
    for i in range(max_retries):
        try:
            r = requests.get(f"{BASE_URL}/health", timeout=5)
            if r.status_code == 200:
                print(f"[OK] Open WebUI is up after {i * delay}s")
                return True
        except Exception:
            pass
        print(f"[..] Waiting for Open WebUI... ({i + 1}/{max_retries})")
        time.sleep(delay)
    print("[ERROR] Open WebUI did not start in time")
    return False


def get_token():
    r = requests.post(
        f"{BASE_URL}/api/v1/auths/signin",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    if r.status_code == 200:
        token = r.json().get("token")
        print(f"[OK] Signed in as {ADMIN_EMAIL}")
        return token

    print(f"[..] Login failed ({r.status_code}), trying signup...")
    r = requests.post(
        f"{BASE_URL}/api/v1/auths/signup",
        json={
            "name": os.environ.get("WEBUI_ADMIN_NAME", "Admin"),
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
        },
        timeout=10,
    )
    if r.status_code in (200, 201):
        token = r.json().get("token")
        print(f"[OK] Admin account created: {ADMIN_EMAIL}")
        return token

    print(f"[ERROR] Cannot get token: {r.status_code} {r.text}")
    return None


def upsert_pipe_function(token):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    for old_id in ("qwen_cbr",):
        r = requests.get(f"{BASE_URL}/api/v1/functions/id/{old_id}", headers=headers, timeout=10)
        if r.status_code == 200:
            requests.delete(f"{BASE_URL}/api/v1/functions/id/{old_id}", headers=headers, timeout=10)
            print(f"[OK] Removed old pipe function: {old_id}")

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

    r = requests.get(f"{BASE_URL}/api/v1/functions/id/{PIPE_FUNCTION_ID}", headers=headers, timeout=10)
    if r.status_code == 200:
        r = requests.post(
            f"{BASE_URL}/api/v1/functions/id/{PIPE_FUNCTION_ID}/update",
            headers=headers, json=payload, timeout=10,
        )
        print(f"[OK] Pipe function updated: {r.status_code}")
    else:
        r = requests.post(
            f"{BASE_URL}/api/v1/functions/create",
            headers=headers, json=payload, timeout=10,
        )
        print(f"[OK] Pipe function created: {r.status_code}")

    if r.status_code not in (200, 201):
        print(f"[WARN] Pipe function response: {r.text[:300]}")
        return

    tr = requests.post(
        f"{BASE_URL}/api/v1/functions/id/{PIPE_FUNCTION_ID}/toggle",
        headers=headers, timeout=10,
    )
    toggled = tr.json() if tr.status_code == 200 else {}
    is_active = toggled.get("is_active")
    if is_active is False:
        tr = requests.post(
            f"{BASE_URL}/api/v1/functions/id/{PIPE_FUNCTION_ID}/toggle",
            headers=headers, timeout=10,
        )
        toggled = tr.json() if tr.status_code == 200 else {}
        is_active = toggled.get("is_active")
    print(f"[OK] Pipe function active: {is_active} (toggle status: {tr.status_code})")


def make_stub_info(url, name="mcp"):
    return {"id": url, "name": name, "version": "0.1.0"}


def sync_mcp_tool_servers(token):
    servers = parse_mcp_servers()
    if not servers:
        print("[SKIP] No MCP servers configured")
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

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
        print(f"[INFO] MCP server from .env: {srv['name']} -> {srv['url']} (path: {srv['path']})")

    r = requests.post(
        f"{BASE_URL}/api/v1/configs/tool_servers",
        headers=headers,
        json={"TOOL_SERVER_CONNECTIONS": new_connections},
        timeout=15,
    )
    if r.status_code in (200, 201):
        for srv in servers:
            print(f"[OK] MCP server synced: {srv['name']} -> {srv['url']} (path: {srv['path']})")
    else:
        print(f"[WARN] POST /configs/tool_servers {r.status_code}: {r.text[:300]}")


def patch_db(db_path):
    """
    Патчит БД напрямую — ТОЛЬКО отключает OpenAI connections.
    MCP connections НЕ трогает.
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
            print(f"[PATCH] Disabled {disabled_count} OpenAI connection(s) in DB")
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
            print("[OK] DB check passed — nothing to patch")

        print(f"[INFO] OpenAI connections in DB: {len(openai_connections)} total, "
              f"{sum(1 for c in openai_connections if c.get('enabled'))} enabled")
        mcp_connections = data.get("tool_server", {}).get("connections", [])
        print(f"[INFO] MCP connections in DB (managed by API): {len(mcp_connections)}")
        for c in mcp_connections:
            print(f"[INFO]   {c.get('url')} path={c.get('path')}")

        conn.close()
    except Exception as e:
        print(f"[ERROR] DB patch failed: {e}")


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
    sync_mcp_tool_servers(token)

    time.sleep(5)
    patch_db(DB_PATH)

    print("[DONE] Init completed successfully")


if __name__ == "__main__":
    main()
