
import ipaddress
from urllib.parse import urlparse


def normalize_server(conf: dict) -> str | None:
    """统一校验 url / host / port，返回可用 URL 或 None"""

    def safe_url(v):
        if isinstance(v, str):
            p = urlparse(v)
            if p.scheme in ("http", "https") and p.netloc:
                return v
        return None

    def safe_host(v):
        if not isinstance(v, str):
            return None
        try:
            ipaddress.ip_address(v)
            return v
        except ValueError:
            if "." in v and all(c.isalnum() or c == "-" or c == "." for c in v):
                return v
        return None

    def safe_port(v):
        try:
            v = int(v)
            return v if 1 <= v <= 65535 else None
        except (TypeError, ValueError):
            return None

    url = safe_url(conf.get("url"))
    if url:
        return url

    host, port = safe_host(conf.get("host")), safe_port(conf.get("port"))
    return f"http://{host}:{port}" if host and port else None
