#!/usr/bin/env python3
"""
Patch open_webui/functions.py:

Root cause:
  json.dumps() fails with 'surrogates not allowed' when the string from the
  upstream LLM contains lone surrogate code points (e.g. \uD83D without pair).
  Python's json module calls str.encode('utf-8') internally which rejects surrogates.

Fix strategy:
  1. Add a _sanitize helper that strips surrogates using encode/decode with errors='replace'
  2. Wrap every `json.dumps(...)` call inside functions.py with sanitize:
       json.dumps(x)  ->  json.dumps(_sanitize_json(x))
  3. Also wrap string res before openai_chat_chunk_message_template:
       res  ->  _sanitize_str(res)
"""

import re
from pathlib import Path

TARGET = Path("/app/backend/open_webui/functions.py")
assert TARGET.exists(), f"Not found: {TARGET}"

src = TARGET.read_text(encoding="utf-8", errors="replace")
original = src

# ── 1. Insert helpers after imports ──────────────────────────────────────────
HELPER = '''

def _sanitize_str(s):
    """Strip lone surrogate code points that break utf-8/json encoding."""
    if not isinstance(s, str):
        return s
    return s.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def _sanitize_json(obj):
    """Recursively sanitize strings in dicts/lists/strings."""
    if isinstance(obj, str):
        return _sanitize_str(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_json(i) for i in obj]
    return obj

'''

if "def _sanitize_str" not in src:
    m = re.search(r'^(def |async def )', src, re.MULTILINE)
    if m:
        src = src[:m.start()] + HELPER + src[m.start():]
        print("[PATCH] Inserted _sanitize_str / _sanitize_json helpers")
    else:
        print("[ERROR] Could not find insertion point")
        import sys; sys.exit(1)
else:
    print("[OK] Helpers already present")

# ── 2. Patch json.dumps(...) -> json.dumps(_sanitize_json(...)) ───────────────
# Match: json.dumps(EXPR) where EXPR is a balanced-paren expression
# We use a simple approach: replace json.dumps( with json.dumps(_sanitize_json(
# and add closing ) before the last ) of each call.
#
# Safer approach: replace the pattern
#   json.dumps(X)
# with
#   json.dumps(_sanitize_json(X))
# by wrapping the argument.

def wrap_json_dumps(text):
    """Replace json.dumps(EXPR) with json.dumps(_sanitize_json(EXPR))."""
    result = []
    i = 0
    MARKER = 'json.dumps('
    WRAPPER = 'json.dumps(_sanitize_json('
    while i < len(text):
        idx = text.find(MARKER, i)
        if idx == -1:
            result.append(text[i:])
            break
        # Check it's not already wrapped
        already = text[idx:idx+len(WRAPPER)] == WRAPPER
        if already:
            result.append(text[i:idx + len(WRAPPER)])
            i = idx + len(WRAPPER)
            continue
        result.append(text[i:idx])
        result.append(WRAPPER)
        # Now find the matching closing paren for the original json.dumps(
        start = idx + len(MARKER)
        depth = 1
        j = start
        while j < len(text) and depth > 0:
            if text[j] == '(':
                depth += 1
            elif text[j] == ')':
                depth -= 1
            j += 1
        # j now points past the closing ')' of json.dumps(EXPR)
        inner = text[start:j-1]   # EXPR
        result.append(inner)
        result.append('))') # close _sanitize_json( and json.dumps(
        i = j
    return ''.join(result)

patched = wrap_json_dumps(src)
count = patched.count('_sanitize_json(')
if count > 0:
    src = patched
    print(f"[PATCH] Wrapped {count} json.dumps() calls with _sanitize_json()")
else:
    print("[WARN] No json.dumps() found to wrap")

# ── 3. Sanitize `res` string before openai_chat_chunk_message_template ────────
# Original:
#   message = openai_chat_chunk_message_template(form_data['model'], res)
# Patched:
#   message = openai_chat_chunk_message_template(form_data['model'], _sanitize_str(res))
OLD3 = r'(openai_chat_chunk_message_template\(form_data\[["\']model["\']\],\s*)(res)(\))'
NEW3 = r'\1_sanitize_str(\2)\3'
c3 = len(re.findall(OLD3, src))
if c3 > 0:
    src = re.sub(OLD3, NEW3, src)
    print(f"[PATCH] Wrapped {c3} openai_chat_chunk_message_template(res) calls")
else:
    print("[INFO] openai_chat_chunk_message_template pattern not found (may already be wrapped)")

# ── Verify & write ─────────────────────────────────────────────────────────────
if src == original:
    print("[WARN] No changes made -- check if already patched or patterns changed")
else:
    TARGET.write_text(src, encoding="utf-8")
    print("[OK] functions.py patched successfully")
    print(f"[OK] Total _sanitize_json wraps: {src.count('_sanitize_json(')}")
    print(f"[OK] Total _sanitize_str wraps: {src.count('_sanitize_str(')}")
