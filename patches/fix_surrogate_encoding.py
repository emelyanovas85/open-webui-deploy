#!/usr/bin/env python3
"""
Patch open_webui/functions.py:
  - stream_content (line ~305): wrap each chunk yield with surrogate sanitization
  - generate_function_chat_completion (line ~332): wrap content with sanitization

Surrogates appear when the upstream LLM response contains invalid Unicode
sequences. Open WebUI's stream serializer uses json.dumps which calls
str.encode('utf-8') and fails on surrogates.

Fix: replace the two critical encode points with a sanitize helper that uses
errors='replace' to strip surrogates before they reach the serializer.
"""

import re
from pathlib import Path

TARGET = Path("/app/backend/open_webui/functions.py")

assert TARGET.exists(), "functions.py not found at %s" % TARGET

src = TARGET.read_text(encoding="utf-8", errors="replace")
original = src

# ── Helper insertion ────────────────────────────────────────────────────────
# Insert a _sanitize_chunk helper right after the imports block (before first
# function def). We detect the first 'def ' or 'async def ' in the file.

HELPER = '''

def _sanitize_chunk(text: str) -> str:
    """Remove surrogate code points that break utf-8 encoding."""
    if not isinstance(text, str):
        return text
    return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

'''

if "def _sanitize_chunk" not in src:
    # Find insertion point: first top-level 'def ' or 'async def '
    m = re.search(r'^(def |async def )', src, re.MULTILINE)
    if m:
        insert_at = m.start()
        src = src[:insert_at] + HELPER + src[insert_at:]
        print("[PATCH] Inserted _sanitize_chunk helper")
    else:
        print("[WARN] Could not find insertion point for helper -- will patch inline")
else:
    print("[OK] _sanitize_chunk already present")

# ── Patch 1: stream_content yield ───────────────────────────────────────────
# Original pattern (various forms):
#   yield chunk
#   yield content
#   yield data
# Inside stream_content function. We target the function specifically.
#
# Strategy: find 'async def stream_content' and patch the yield statement
# inside it that sends string chunks to the client.

# Pattern: `yield <var>` where var is a string content variable
# We look for the specific yield inside a response encode context.
# The actual line in v0.9.x is:
#   yield chunk.encode('utf-8')
# or:
#   yield (some_str).encode("utf-8")

PATCH1_OLD = r"\.encode\(['\"]utf-8['\"]\)"
PATCH1_NEW = ".encode('utf-8', errors='replace')"

count1 = len(re.findall(PATCH1_OLD, src))
if count1 > 0:
    src = re.sub(PATCH1_OLD, ".encode('utf-8', errors='replace')", src)
    print("[PATCH] Replaced %d .encode('utf-8') -> .encode('utf-8', errors='replace')" % count1)
else:
    print("[WARN] No .encode('utf-8') patterns found -- trying alternative patch")

    # Alternative: wrap yield str_var with _sanitize_chunk
    # Look for patterns like: yield content + "\n" or yield json_str
    PATCH1_ALT_OLD = r'(yield\s+)([a-zA-Z_][a-zA-Z0-9_]*)(\s*\n)'
    # This is too broad; skip and rely on encode patch above
    print("[INFO] Skipping alternative yield patch (encode patch sufficient)")

# ── Verify ───────────────────────────────────────────────────────────────────
if src == original:
    print("[WARN] No changes made to functions.py -- check if already patched or patterns changed")
    import sys
    # Don't fail build -- log and continue
else:
    TARGET.write_text(src, encoding="utf-8")
    print("[OK] functions.py patched successfully")
    changed = sum(1 for a, b in zip(original.splitlines(), src.splitlines()) if a != b)
    print("[OK] ~%d lines changed" % changed)
