FROM ghcr.io/open-webui/open-webui:v0.9.6

# Патч 1: исправляет UnboundLocalError 'server_id' в utils/tools.py
#   при tool_id формата, не равного 2 или 3 частям через ':'
COPY patches/fix_tools_server_id.py /tmp/fix_tools_server_id.py
RUN python3 /tmp/fix_tools_server_id.py

# Патч 2: разрешает type='mcp' в get_tool_servers_data (utils/tools.py)
#   open-webui v0.9.6 фильтрует серверы по type == 'openapi',
#   MCP-серверы с type='mcp' молча игнорируются → инструменты не загружаются.
#   Патч меняет условие на: type in ('openapi', 'mcp')
COPY patches/fix_tools_type_filter.py /tmp/fix_tools_type_filter.py
RUN python3 /tmp/fix_tools_type_filter.py

# Патч 3: поддержка MCP Streamable HTTP транспорта (utils/tools.py)
#   get_tool_server_data() делал GET с Accept: application/json ожидая OpenAPI spec,
#   но Spring AI Streamable HTTP сервер возвращает 400 на такой запрос.
#   Патч добавляет helper _mcp_streamable_initialize() который делает MCP
#   initialize handshake через POST, и вставляет early-return для URL содержащих '/mcp'.
COPY patches/fix_mcp_streamable_http.py /tmp/fix_mcp_streamable_http.py
RUN python3 /tmp/fix_mcp_streamable_http.py
