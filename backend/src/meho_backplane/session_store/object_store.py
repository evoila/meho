# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The S3-compatible object storage of the session store.

:class:`ObjectStore` is the small interface the session store needs: put,
get and delete whole objects. :class:`S3ObjectStore` implements it with
boto3 against any store with the Amazon S3 interface (for example MinIO).

* boto3 is synchronous, so each call runs in a worker thread.
* The access keys are read from Vault for every client
  (:func:`~meho_backplane.scheduler.vault_credentials.read_service_secret`),
  so a rotated key is used at once.
* Path-style addressing, and checksums only where the API requires them:
  many S3-compatible stores refuse the default checksum headers that newer
  boto3 releases send.

Tests replace the store with :func:`set_object_store_for_testing`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from meho_backplane.scheduler.vault_credentials import (
    SchedulerVaultBrokerError,
    read_service_secret,
)
from meho_backplane.session_store.config import SessionStoreConfig, load_config

__all__ = [
    "ObjectStore",
    "ObjectStoreError",
    "S3ObjectStore",
    "SessionStoreNotConfiguredError",
    "get_object_store",
    "set_object_store_for_testing",
]

#: Field names of the Vault secret that holds the access keys.
ACCESS_KEY_FIELD = "access_key_id"
SECRET_KEY_FIELD = "secret_access_key"

#: S3 ``DeleteObjects`` takes at most 1000 keys per call.
_DELETE_BATCH = 1000


class SessionStoreNotConfiguredError(Exception):
    """The endpoint, the bucket or the credentials path is not set."""


class ObjectStoreError(Exception):
    """The object storage refused or failed a call. The text holds no secret."""


class ObjectStore(Protocol):
    """Put, get and delete whole objects by key."""

    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, keys: Sequence[str]) -> None: ...


CredentialReader = Callable[[], Awaitable[tuple[str, str]]]


def _vault_credentials(config: SessionStoreConfig) -> CredentialReader:
    async def read() -> tuple[str, str]:
        try:
            fields = await read_service_secret(
                config.credentials_vault_path, check="session_store_credentials_read"
            )
        except SchedulerVaultBrokerError as exc:
            raise ObjectStoreError(f"cannot read the object storage keys: {exc}") from exc
        access = (fields or {}).get(ACCESS_KEY_FIELD, "").strip()
        secret = (fields or {}).get(SECRET_KEY_FIELD, "").strip()
        if not access or not secret:
            raise ObjectStoreError(
                "the Vault secret of the object storage has no "
                f"{ACCESS_KEY_FIELD} and {SECRET_KEY_FIELD}"
            )
        return access, secret

    return read


class S3ObjectStore:
    """:class:`ObjectStore` on an S3-compatible service, with boto3."""

    def __init__(
        self,
        config: SessionStoreConfig,
        *,
        credentials: CredentialReader | None = None,
        client_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        self._config = config
        self._credentials = credentials or _vault_credentials(config)
        self._client_factory = client_factory or self._new_client

    def _new_client(self, access_key: str, secret_key: str) -> Any:
        return boto3.client(
            "s3",
            endpoint_url=self._config.endpoint,
            region_name=self._config.region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                s3={"addressing_style": "path"},
                signature_version="s3v4",
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                connect_timeout=10,
                read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    async def _client(self) -> Any:
        access_key, secret_key = await self._credentials()
        return self._client_factory(access_key, secret_key)

    async def _call(self, what: str, call: Callable[[], Any]) -> Any:
        try:
            return await asyncio.to_thread(call)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "unknown")
            raise ObjectStoreError(f"object storage refused {what}: {code}") from exc
        except BotoCoreError as exc:
            raise ObjectStoreError(f"object storage failed {what}: {type(exc).__name__}") from exc

    async def put(self, key: str, data: bytes) -> None:
        """Store *data* under *key*. One request: the object is whole or absent."""
        client = await self._client()
        await self._call(
            "put",
            lambda: client.put_object(
                Bucket=self._config.bucket, Key=key, Body=data, ContentType="application/gzip"
            ),
        )

    async def get(self, key: str) -> bytes:
        """Return the bytes stored under *key*."""
        client = await self._client()

        def read() -> bytes:
            response = client.get_object(Bucket=self._config.bucket, Key=key)
            body: bytes = response["Body"].read()
            return body

        result: bytes = await self._call("get", read)
        return result

    async def delete(self, keys: Sequence[str]) -> None:
        """Delete *keys*. A key that is already gone is not an error."""
        if not keys:
            return
        client = await self._client()
        for start in range(0, len(keys), _DELETE_BATCH):
            batch = [{"Key": key} for key in keys[start : start + _DELETE_BATCH]]

            def delete_batch(batch: list[dict[str, str]] = batch) -> Any:
                return client.delete_objects(
                    Bucket=self._config.bucket, Delete={"Objects": batch, "Quiet": True}
                )

            response = await self._call("delete", delete_batch)
            errors = response.get("Errors") or []
            if errors:
                code = errors[0].get("Code", "unknown")
                raise ObjectStoreError(f"object storage refused delete: {code}")


_override: ObjectStore | None = None


def set_object_store_for_testing(store: ObjectStore | None) -> None:
    """Use *store* instead of the configured S3 store (``None`` resets)."""
    global _override
    _override = store


def get_object_store(config: SessionStoreConfig | None = None) -> ObjectStore:
    """Return the object store, or raise when the store is not configured."""
    if _override is not None:
        return _override
    config = config or load_config()
    if not config.configured:
        raise SessionStoreNotConfiguredError(
            "set SESSION_STORE_ENDPOINT, SESSION_STORE_BUCKET and "
            "SESSION_STORE_CREDENTIALS_VAULT_PATH"
        )
    return S3ObjectStore(config)
