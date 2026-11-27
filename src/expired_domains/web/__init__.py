"""FastAPI web interface for the expired-domain toolkit."""

from expired_domains.web.app import create_app

__all__ = ["create_app"]
