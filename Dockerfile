FROM ghcr.io/open-webui/open-webui:v0.9.6

# ─── Compatibility check ───────────────────────────────────────────────────────────────────────
COPY patches/check_patch_compat.py /tmp/check_patch_compat.py
RUN python3 /tmp/check_patch_compat.py

# ─── Patch 1 ──────────────────────────────────────────────────────────────────────────────
# Fixes UnboundLocalError 'server_id' in utils/tools.py
COPY patches/fix_tools_server_id.py /tmp/fix_tools_server_id.py
RUN python3 /tmp/fix_tools_server_id.py

# ─── Patch 2 ──────────────────────────────────────────────────────────────────────────────
# Allows type='mcp' in get_tool_servers_data (utils/tools.py).
COPY patches/fix_tools_type_filter.py /tmp/fix_tools_type_filter.py
RUN python3 /tmp/fix_tools_type_filter.py

# ─── Patch 3 ──────────────────────────────────────────────────────────────────────────────
# MCP Streamable HTTP transport support (utils/tools.py).
COPY patches/fix_mcp_streamable_http.py /tmp/fix_mcp_streamable_http.py
RUN python3 /tmp/fix_mcp_streamable_http.py

# ─── Patch 4 ──────────────────────────────────────────────────────────────────────────────
# Fixes 'utf-8' codec can't encode surrogates in functions.py
# stream_content and generate_function_chat_completion.
# Replaces all .encode('utf-8') with .encode('utf-8', errors='replace')
# so surrogate characters from upstream LLM responses don't crash the stream.
COPY patches/fix_surrogate_encoding.py /tmp/fix_surrogate_encoding.py
RUN python3 /tmp/fix_surrogate_encoding.py
