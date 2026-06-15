FROM python:3.11-slim
RUN pip install requests --quiet
COPY init-openwebui.py /scripts/init-openwebui.py
CMD ["python", "/scripts/init-openwebui.py"]
