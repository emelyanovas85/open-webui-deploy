#!/usr/bin/env python3
"""
init-openwebui.py — запускается один раз при старте через open-webui-init контейнер.

Что делает:
1. Ждёт пока Open WebUI поднимется
2. Получает JWT-токен администратора (логин или создание)
3. Создаёт/обновляет Pipe Function с SSL-обходом и streaming
4. Подключает MCP Tool Servers (SSE) — поддерживает несколько через MCP_SERVER_URLS
   Если сервер уже зарегистрирован с неправильным типом (openapi вместо mcp) —
   удаляет и перерегистрирует с type=mcp.
"""

import os
import sys
import time
import json
import requests

BASE_URL = os.environ.get("WEBUI_BASE_URL", "http://open-webui:8080").rstrip("/")
ADMIN_EMAIL = os.environ.get("WEBUI_ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.environ.get("WEBUI_ADMIN_PASSWORD", "")

# Поддержка нескольких MCP серверов через MCP_SERVER_URLS
# Формат: url1::name1::desc1,url2::name2::desc2
# Или старый формат: просто URL (обратная совместимость через MCP_SERVER_URL)
MCP_SERVER_URLS_RAW = os.environ.get("MCP_SERVER_URLS", "")
MCP_SERVER_URL_LEGACY = os.environ.get("MCP_SERVER_URL", "")


def parse_mcp_servers():
    """Парсим список MCP серверов из переменных окружения."""
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


PIPE_FUNCTION_ID = "qwen_cbr"
PIPE_FUNCTION_NAME = "Qwen3.5 CBR"

PIPE_FUNCTION_CODE = '''
"""
title: Qwen3_5_CBR
author: local
version: 1.2
"""

import httpx
import ssl
import json

UPSTREAM_URL = "https://chat.ehd-zr.cbr.ru/openai/chat/completions"
MODEL_ID = "Qwen/Qwen3.5-397B-A17B-GPTQ-Int4"
API_KEY = "sk-09fd660cdc8640ac861fe85a16d2d2f1"


def get_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class Pipe:
    def __init__(self):
        self.id = "qwen_cbr"
        self.name = "Qwen3.5 CBR"
        self.type = "manifold"

    def pipes(self):
        return [{"id": "qwen_cbr", "name": "Qwen3.5 CBR"}]

    def _build_payload(self, body: dict):
        payload = {
            "model": MODEL_ID,
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
            with client.stream("POST", UPSTREAM_URL, headers=headers, json=payload) as r:
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
            r = client.post(UPSTREAM_URL, headers=headers, json=payload)
            r.raise_for_status()
            result = r.json()
            return result["choices"][0]["message"]["content"]
'''


def wait_for_webui(max_retries=30, delay=5):
    """Ждём пока Open WebUI станет доступен."""
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
    """Получаем JWT-токен администратора."""
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
    """Создаём или обновляем Pipe Function.

    Open WebUI v0.8+:
      - создание:   POST /api/v1/functions/create
      - обновление: PUT  /api/v1/functions/{id}   ← не POST /{id}/update
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    r = requests.get(f"{BASE_URL}/api/v1/functions/{PIPE_FUNCTION_ID}", headers=headers, timeout=10)
    payload = {
        "id": PIPE_FUNCTION_ID,
        "name": PIPE_FUNCTION_NAME,
        "content": PIPE_FUNCTION_CODE,
        "meta": {"description": "Qwen3.5 CBR via internal SSL"},
    }

    if r.status_code == 200:
        # PUT /api/v1/functions/{id} — правильный эндпоинт обновления в v0.8+
        r = requests.put(
            f"{BASE_URL}/api/v1/functions/{PIPE_FUNCTION_ID}",
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


def add_mcp_tool_servers(token):
    """Добавляем MCP SSE tool servers (поддерживает несколько).
    Если сервер зарегистрирован с неправильным типом (openapi вместо mcp) —
    удаляем и перерегистрируем с type=mcp.
    """
    servers = parse_mcp_servers()
    if not servers:
        print("[SKIP] No MCP servers configured (MCP_SERVER_URLS not set), skipping")
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Получаем список уже зарегистрированных серверов: {url -> server_dict}
    existing = {}
    r = requests.get(f"{BASE_URL}/api/v1/tools/servers", headers=headers, timeout=10)
    if r.status_code == 200:
        try:
            data = r.json()
            if isinstance(data, list):
                for srv in data:
                    existing[srv.get("url", "")] = srv
            else:
                print(f"[WARN] Unexpected format from /tools/servers: {type(data)}")
        except Exception as e:
            print(f"[WARN] Could not parse /tools/servers: {e} — body: {r.text[:200]}")
    else:
        print(f"[WARN] GET /tools/servers returned {r.status_code}: {r.text[:200]}")

    for srv in servers:
        url = srv["url"]
        payload = {
            "name": srv["name"],
            "url": url,
            "description": srv["description"],
            "auth_type": "none",
            "type": "mcp",
        }

        if url in existing:
            existing_type = existing[url].get("type", "")
            if existing_type == "mcp":
                print(f"[OK] MCP server already registered correctly: {url}")
                continue
            # Тип неправильный (openapi) — удаляем и перерегистрируем
            server_id = existing[url].get("id", "")
            if server_id:
                rd = requests.delete(
                    f"{BASE_URL}/api/v1/tools/servers/{server_id}",
                    headers=headers, timeout=10,
                )
                print(f"[..] Deleted old server {url} (type={existing_type}): {rd.status_code}")
            else:
                print(f"[WARN] Cannot delete server {url}: no id in response, will try to add anyway")

        r = requests.post(
            f"{BASE_URL}/api/v1/tools/servers",
            headers=headers, json=payload, timeout=15,
        )
        if r.status_code in (200, 201):
            print(f"[OK] MCP server added: {url} (name={srv['name']})")
        else:
            print(f"[WARN] MCP server {url}: {r.status_code} {r.text[:300]}")


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
    add_mcp_tool_servers(token)
    print("[DONE] Init completed successfully")


if __name__ == "__main__":
    main()
