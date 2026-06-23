FROM ghcr.io/open-webui/open-webui:v0.9.6

# ─── Compatibility check ─────────────────────────────────────────────────────
# Runs BEFORE any patch. Verifies that tools.py structure is compatible
# with all three patches. Fails the build with a clear error if open-webui
# was upgraded and the structure changed.
COPY patches/check_patch_compat.py /tmp/check_patch_compat.py
RUN python3 /tmp/check_patch_compat.py

# ─── Patch 1 ─────────────────────────────────────────────────────────────────
# Fixes UnboundLocalError 'server_id' in utils/tools.py
# when tool_id format is not the expected 2 or 3 parts split by ':'.
COPY patches/fix_tools_server_id.py /tmp/fix_tools_server_id.py
RUN python3 /tmp/fix_tools_server_id.py

# ─── Patch 2 ─────────────────────────────────────────────────────────────────
# Allows type='mcp' in get_tool_servers_data (utils/tools.py).
# open-webui v0.9.6 filters servers by type == 'openapi' — MCP servers
# with type='mcp' are silently ignored and tools are never loaded.
# Patch changes the condition to: type in ('openapi', 'mcp')
COPY patches/fix_tools_type_filter.py /tmp/fix_tools_type_filter.py
RUN python3 /tmp/fix_tools_type_filter.py

# ─── Patch 3 (v4) ────────────────────────────────────────────────────────────
# MCP Streamable HTTP transport support (utils/tools.py).
# Changes from v3:
#   - MCP detection is no longer based on '/mcp' in URL path.
#     Now probes the URL with an MCP initialize request and checks
#     for MCP response signature (serverInfo / protocolVersion).
#     Falls through to OpenAPI path if probe fails or response is plain JSON.
#   - Protocol version negotiation: tries '2025-03-26' first,
#     falls back to '2024-11-05' on mismatch error.
COPY patches/fix_mcp_streamable_http.py /tmp/fix_mcp_streamable_http.py
RUN python3 /tmp/fix_mcp_streamable_http.py
