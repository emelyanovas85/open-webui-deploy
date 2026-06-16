# sitecustomize.py — выполняется Python при старте интерпретатора
# Применяем SSL патч для совместимости с Bank of Russia TLS (AES256-GCM-SHA384)
try:
    import ssl_patch  # noqa: F401
except Exception:
    pass
