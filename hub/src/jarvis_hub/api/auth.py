import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from jarvis_hub.config import Settings, Source, get_settings

_bearer = HTTPBearer(auto_error=False)


def source_for_token(token: str, settings: Settings) -> Source | None:
    for known, source in settings.token_sources().items():
        if secrets.compare_digest(token.encode(), known.encode()):
            return source
    return None


def require_source(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> Source:
    """Resolve the caller's source from its bearer token. The token, not the body, decides."""
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    source = source_for_token(creds.credentials, settings)
    if source is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    return source
