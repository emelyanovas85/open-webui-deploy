FROM ghcr.io/open-webui/open-webui:v0.9.6

# Патч: исправляет UnboundLocalError 'server_id' в utils/tools.py
# Используем COPY + python3 вместо heredoc — heredoc ненадёжно работает в legacy builder
COPY patches/fix_tools_server_id.py /tmp/fix_tools_server_id.py
RUN python3 /tmp/fix_tools_server_id.py
