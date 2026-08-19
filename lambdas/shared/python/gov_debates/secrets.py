"""Secrets Manager lookup for per-jurisdiction API keys, cached per ARN.

Only two of the ten sources need a credential (Germany DIP, US GovInfo); the rest are
open. The original agent used ``@lru_cache(maxsize=1)`` around a single global key — wrong
here, because different Lambdas read different secrets. This caches per ARN instead.

Each Lambda is granted read on ONLY its own secret (enforced in CDK), so this module never
holds another jurisdiction's credential. Values are cached for the container lifetime and
never logged.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Optional


@lru_cache(maxsize=16)
def _fetch(secret_arn: str) -> str:
    import boto3  # imported lazily so unit tests don't require boto3/network

    region = os.getenv("AWS_REGION", "eu-central-1")
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=secret_arn)
    raw = resp.get("SecretString")
    if not raw:
        raise RuntimeError(f"secret {secret_arn} has no SecretString")
    return raw


def get_api_key(
    secret_arn: Optional[str] = None,
    *,
    json_key: str = "apiKey",
    env_var: Optional[str] = None,
) -> str:
    """Return an API key.

    Resolution order:
      1. ``secret_arn`` (or the ``*_SECRET_ARN`` env var) -> fetch from Secrets Manager.
         The secret is JSON ``{"<json_key>": "..."}`` or a plain string.
      2. ``env_var`` (local dev / tests only) -> read the key directly from the environment.

    Raises RuntimeError when no key can be resolved.
    """
    arn = secret_arn or os.getenv("SECRET_ARN")
    if arn:
        raw = _fetch(arn)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw  # allow a plain-string secret
        key = data.get(json_key)
        if not key:
            raise RuntimeError(f"secret {arn} JSON missing {json_key!r}")
        return key

    if env_var:
        direct = os.getenv(env_var)
        if direct:
            return direct

    raise RuntimeError(
        "No API key available: set SECRET_ARN (prod) "
        + (f"or {env_var} (dev)." if env_var else "for this jurisdiction.")
    )


def clear_cache() -> None:
    """Clear the per-ARN cache (used by tests)."""
    _fetch.cache_clear()
