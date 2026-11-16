"""Bearer-token authentication against EVAL_ENGINE_API_KEY."""

import secrets

from fastapi import Depends, Request

from app.config import Settings
from app.errors import ApiError


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def require_api_key(request: Request, settings: Settings = Depends(get_app_settings)) -> None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    expected = settings.eval_engine_api_key.get_secret_value()
    # compare_digest takes the same time whether the first or last character differs,
    # so response timing does not reveal how much of the key a caller guessed right.
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.strip().encode(), expected.encode()):
        raise ApiError(
            401,
            "UNAUTHORIZED",
            "Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
