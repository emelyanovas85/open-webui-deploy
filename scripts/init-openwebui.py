#!/usr/bin/env python3
"""
init-openwebui.py — запускается один раз при старте через open-webui-init контейнер.

Что делает:
1. Ждёт пока Open WebUI поднимется
2. Получает JWT-токен администратора (логин или создание)
3. Создаёт/обновляет Pipe Function с SSL-обходом и streaming
4. Подключает MCP Tool Server (SSE)
"""

import os
import sys
import time
import json
import requests

BASE_URL = os.environ.get("WEBUI_BASE_URL", "http://open-webui:8080").rstrip("/")
ADMIN_EMAIL = os.environ.get("WEBUI_ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.environ.get("WEBUI_ADMIN_PASSWORD", "")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "")

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
    # Пробуем войти
    r = requests.post(
        f"{BASE_URL}/api/v1/auths/signin",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    if r.status_code == 200:
        token = r.json().get("token")
        print(f"[OK] Signed in as {ADMIN_EMAIL}")
        return token

    # Если пользователь ещё не создан — регистрируем первого (он станет admin)
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
    """Создаём или обновляем Pipe Function."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Проверяем существует ли уже
    r = requests.get(f"{BASE_URL}/api/v1/functions/{PIPE_FUNCTION_ID}", headers=headers, timeout=10)
    payload = {
        "id": PIPE_FUNCTION_ID,
        "name": PIPE_FUNCTION_NAME,
        "content": PIPE_FUNCTION_CODE,
        "meta": {"description": "Qwen3.5 CBR via internal SSL"},
    }

    if r.status_code == 200:
        # Обновляем
        r = requests.post(
            f"{BASE_URL}/api/v1/functions/{PIPE_FUNCTION_ID}/update",
            headers=headers, json=payload, timeout=10,
        )
        print(f"[OK] Pipe function updated: {r.status_code}")
    else:
        # Создаём
        r = requests.post(
            f"{BASE_URL}/api/v1/functions/create",
            headers=headers, json=payload, timeout=10,
        )
        print(f"[OK] Pipe function created: {r.status_code}")

    if r.status_code not in (200, 201):
        print(f"[WARN] Pipe function response: {r.text[:300]}")


def add_mcp_tool_server(token):
    """Добавляем MCP SSE tool server."""
    if not MCP_SERVER_URL:
        print("[SKIP] MCP_SERVER_URL not set, skipping")
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Получаем список существующих tool servers
    r = requests.get(f"{BASE_URL}/api/v1/tools/servers", headers=headers, timeout=10)
    if r.status_code == 200:
        existing = r.json()
        for srv in existing:
            if srv.get("url") == MCP_SERVER_URL:
                print(f"[OK] MCP tool server already registered: {MCP_SERVER_URL}")
                return

    # Добавляем новый — тип mcp для SSE-серверов (supergateway)
    payload = {
        "name": "gitlab_mcp",
        "url": MCP_SERVER_URL,
        "description": "GitLab MCP (SSE via supergateway)",
        "auth_type": "none",
        "type": "mcp",
    }
    r = requests.post(
        f"{BASE_URL}/api/v1/tools/servers",
        headers=headers, json=payload, timeout=15,
    )
    if r.status_code in (200, 201):
        print(f"[OK] MCP tool server added: {MCP_SERVER_URL}")
    else:
        print(f"[WARN] MCP tool server: {r.status_code} {r.text[:300]}")


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
    add_mcp_tool_server(token)
    print("[DONE] Init completed successfully")


if __name__ == "__main__":
    main()
