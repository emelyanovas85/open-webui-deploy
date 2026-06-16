#!/usr/bin/env python3
"""
init-openwebui.py — запускается один раз при старте через open-webui-init контейнер.

Что делает:
1. Ждёт пока Open WebUI поднимется
2. Получает JWT-токен администратора
3. Отключает дефолтный OpenAI connection (чтобы не было Connection error на api.openai.com)
4. Создаёт/обновляет Pipe Function — модели грузятся динамически через GET /openai/models
5. Подключает MCP Tool Servers (с stub info чтобы configs.py:205 не падал)
6. Патчит БД если MCP info всё ещё null (tools.py:118 защита)
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


def parse_mcp_servers():
    servers = []
    if MCP_SERVER_URLS_RAW:
        for entry in MCP_SERVER_URLS_RAW.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split("::")
            url = parts[0].strip()
            name = parts[1].strip() if len(parts) > 1 else url.split("/")[2].replace(":", "_")
            desc = parts[2].strip() if len(parts) > 2 else name
            servers.append({"url": url, "name": name, "description": desc})
    elif MCP_SERVER_URL_LEGACY:
        servers.append({
            "url": MCP_SERVER_URL_LEGACY,
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
version: 3.0
description: Динамический список моделей с chat.ehd-zr.cbr.ru через SSL-совместимый Pipe.
"""

import httpx
import ssl
import json

UPSTREAM_BASE = "https://chat.ehd-zr.cbr.ru/openai"
API_KEY = "sk-09fd660cdc8640ac861fe85a16d2d2f1"
MODELS_CACHE = []


def get_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def fetch_models():
    """Запрашивает список моделей напрямую у upstream (с SSL-патчем)."""
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
    except Exception as e:
        if not MODELS_CACHE:
            MODELS_CACHE = [
                {"id": "Qwen/Qwen3.5-397B-A17B-GPTQ-Int4", "name": "Qwen3.5 397B (fallback)"},
            ]
    return MODELS_CACHE


class Pipe:
    def __init__(self):
        self.id = "cbr_models"
        self.name = "CBR Models"
        self.type = "manifold"

    def pipes(self):
        """Вызывается Open WebUI при загрузке — загружает актуальный список моделей."""
        models = fetch_models()
        return [{"id": m["id"], "name": m["name"]} for m in models]

    def _resolve_model_id(self, body: dict) -> str:
        """Open WebUI передаёт model как "cbr_models.{model_id}" — извлекаем оригинал."""
        raw = body.get("model", "")
        prefix = f"{self.id}."
        if raw.startswith(prefix):
            return raw[len(prefix):]
        return raw

    def _build_payload(self, body: dict) -> dict:
        payload = {
            "model": self._resolve_model_id(body),
            "messages": body.get("messages", []),
        }
        for key in ("temperature", "max_tokens", "top_p",
                    "presence_penalty", "frequency_penalty"):
            if key in body:
                payload[key] = body[key]
        if body.get("stream"):
            payload["stream"] = True
        return payload

    def _stream_response(self, payload: dict):
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        ssl_ctx = get_ssl_context()
        with httpx.Client(verify=ssl_ctx, timeout=120.0) as client:
            with client.stream(
                "POST",
                f"{UPSTREAM_BASE}/chat/completions",
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
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    content = choices[0].get("delta", {}).get("content")
                    if content:
                        yield content

    def pipe(self, body: dict):
        payload = self._build_payload(body)
        if body.get("stream"):
            return self._stream_response(payload)
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
        ssl_ctx = get_ssl_context()
        with httpx.Client(verify=ssl_ctx, timeout=120.0) as client:
            r = client.post(
                f"{UPSTREAM_BASE}/chat/completions",
                headers=headers,
                json=payload,
            )
            r.raise_for_status()
            result = r.json()
            return result["choices"][0]["message"]["content"]
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


def disable_openai_connection(token):
    """
    Отключает дефолтный OpenAI connection.
    Open WebUI при первом старте записывает в БД дефолтный endpoint https://api.openai.com/v1,
    даже если OPENAI_API_BASE_URL='' в .env.
    Нужно явно выставить enabled=false через API.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Получаем текущие connections
    r = requests.get(f"{BASE_URL}/api/v1/configs/openai", headers=headers, timeout=10)
    if r.status_code != 200:
        print(f"[WARN] GET /configs/openai returned {r.status_code} — skipping")
        return

    try:
        data = r.json()
    except Exception as e:
        print(f"[WARN] Could not parse /configs/openai: {e}")
        return

    connections = data.get("OPENAI_API_CONNECTIONS", [])
    if not connections:
        print("[OK] No OpenAI connections found — nothing to disable")
        return

    # Отключаем все connections
    for conn in connections:
        conn["enabled"] = False

    r = requests.post(
        f"{BASE_URL}/api/v1/configs/openai",
        headers=headers,
        json={"OPENAI_API_CONNECTIONS": connections},
        timeout=10,
    )
    if r.status_code in (200, 201):
        print(f"[OK] OpenAI connections disabled ({len(connections)} connection(s))")
    else:
        print(f"[WARN] POST /configs/openai {r.status_code}: {r.text[:300]}")


def upsert_pipe_function(token):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # Удаляем старый qwen_cbr если есть
    for old_id in ("qwen_cbr",):
        r = requests.get(f"{BASE_URL}/api/v1/functions/id/{old_id}", headers=headers, timeout=10)
        if r.status_code == 200:
            requests.delete(f"{BASE_URL}/api/v1/functions/id/{old_id}", headers=headers, timeout=10)
            print(f"[OK] Removed old pipe function: {old_id}")

    payload = {
        "id": PIPE_FUNCTION_ID,
        "name": PIPE_FUNCTION_NAME,
        "content": PIPE_FUNCTION_CODE,
        "meta": {"description": "Dynamic CBR models via SSL-compatible Pipe"},
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


def make_stub_info(url):
    return {"id": url, "name": "mcp", "version": "0.1.0"}


def add_mcp_tool_servers(token):
    servers = parse_mcp_servers()
    if not servers:
        print("[SKIP] No MCP servers configured")
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    r = requests.get(f"{BASE_URL}/api/v1/configs/tool_servers", headers=headers, timeout=10)
    if r.status_code != 200:
        print(f"[WARN] GET /configs/tool_servers returned {r.status_code}: {r.text[:200]}")
        existing_connections = []
    else:
        try:
            existing_connections = r.json().get("TOOL_SERVER_CONNECTIONS", [])
        except Exception as e:
            print(f"[WARN] Could not parse /configs/tool_servers: {e}")
            existing_connections = []

    for c in existing_connections:
        if c.get("info") is None:
            c["info"] = make_stub_info(c.get("url", ""))

    existing_urls = {c.get("url", "") for c in existing_connections}
    new_connections = list(existing_connections)
    added = []

    for srv in servers:
        url = srv["url"]
        try:
            from urllib.parse import urlparse
            path = urlparse(url).path or "/sse"
        except Exception:
            path = "/sse"

        if url in existing_urls:
            print(f"[OK] MCP server already registered: {url}")
            continue

        new_connections.append({
            "url": url,
            "path": path,
            "type": "mcp",
            "auth_type": "none",
            "key": "",
            "headers": None,
            "config": {"enable": True},
            "info": make_stub_info(url),
        })
        added.append(url)

    if not added and not any(c.get("info") is None for c in existing_connections):
        print("[OK] All MCP servers already registered")
        return

    r = requests.post(
        f"{BASE_URL}/api/v1/configs/tool_servers",
        headers=headers,
        json={"TOOL_SERVER_CONNECTIONS": new_connections},
        timeout=15,
    )
    if r.status_code in (200, 201):
        for url in added:
            print(f"[OK] MCP server added: {url}")
        if not added:
            print("[OK] MCP connections updated (stub info patched)")
    else:
        print(f"[WARN] POST /configs/tool_servers {r.status_code}: {r.text[:300]}")


def patch_db_null_info():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT id, data FROM config ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            conn.close()
            return
        config_id, raw = row
        data = json.loads(raw)
        connections = data.get("tool_server", {}).get("connections", [])
        patched = False
        for c in connections:
            if c.get("info") is None:
                c["info"] = make_stub_info(c.get("url", ""))
                patched = True
                print(f"[PATCH] Set stub info for {c['url']}")
        if patched:
            cur.execute(
                "UPDATE config SET data = ? WHERE id = ?",
                (json.dumps(data), config_id)
            )
            conn.commit()
            print("[OK] DB patched successfully")
        else:
            print("[OK] DB check passed — no null info found")
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

    disable_openai_connection(token)
    upsert_pipe_function(token)
    add_mcp_tool_servers(token)
    time.sleep(3)
    patch_db_null_info()
    print("[DONE] Init completed successfully")


if __name__ == "__main__":
    main()
