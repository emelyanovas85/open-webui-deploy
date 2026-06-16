"""ssl_patch.py — monkey-patch ssl.create_default_context чтобы aiohttp
использовал SECLEVEL=0 и принимал AES256-GCM-SHA384 без ECDHE.

Устанавливается как sitecustomize через PYTHONPATH=/app/scripts.
"""
import ssl as _ssl

_orig = _ssl.create_default_context

def _patched_create_default_context(purpose=_ssl.Purpose.SERVER_AUTH, *args, **kwargs):
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    try:
        ctx.set_ciphers('DEFAULT:@SECLEVEL=0')
    except _ssl.SSLError:
        pass
    ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
    return ctx

_ssl.create_default_context = _patched_create_default_context
