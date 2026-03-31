# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS Connector using boto3 SDK.

Implements the BaseConnector interface using mixin pattern for organization.
Uses boto3 for AWS API access. Since boto3 is synchronous, all API calls
use asyncio.to_thread() to avoid blocking the event loop.

Handler mixins provide all service-specific operation implementations.
"""

import asyncio
import time
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import (
    BaseConnector,
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)
from meho_app.modules.connectors.aws.handlers import (
    CloudWatchHandlerMixin,
    EC2HandlerMixin,
    ECSHandlerMixin,
    EKSHandlerMixin,
    LambdaHandlerMixin,
    RDSHandlerMixin,
    S3HandlerMixin,
    VPCHandlerMixin,
)
from meho_app.modules.connectors.aws.operations import AWS_OPERATIONS
from meho_app.modules.connectors.aws.types import AWS_TYPES

logger = get_logger(__name__)


class AWSConnector(
    BaseConnector,
    CloudWatchHandlerMixin,
    EC2HandlerMixin,
    ECSHandlerMixin,
    EKSHandlerMixin,
    S3HandlerMixin,
    LambdaHandlerMixin,
    RDSHandlerMixin,
    VPCHandlerMixin,
):
    """
    Amazon Web Services connector using boto3 SDK.

    Provides native access to AWS for:
    - EC2: Instance management
    - CloudWatch: Metrics and alarms
    - ECS: Container service management
    - EKS: Kubernetes cluster management
    - S3: Object storage
    - Lambda: Serverless functions
    - RDS: Managed databases

    Organization:
    - Handler mixins: 8 service-specific mixins in handlers/
    - Serializers: in serializers.py
    - Helpers: in helpers.py (ARN parsing, tag normalization)
    - Types: in types.py (entity type definitions)

    Example:
        connector = AWSConnector(
            connector_id="abc123",
            config={
                "default_region": "us-east-1",
            },
            credentials={
                "aws_access_key_id": "AKIA...",
                "aws_secret_access_key": "...",
            }
        )

        async with connector:
            result = await connector.execute("list_ec2_instances", {})
            print(result.data)
    """

    def __init__(self, connector_id: str, config: dict[str, Any], credentials: dict[str, Any]):
        super().__init__(connector_id, config, credentials)

        # Configuration
        self._default_region = config.get("default_region", "us-east-1")

        # boto3 session and service clients (initialized on connect)
        self._session: Any = None
        self._ec2_client: Any = None
        self._cloudwatch_client: Any = None
        self._ecs_client: Any = None
        self._eks_client: Any = None
        self._s3_client: Any = None
        self._lambda_client: Any = None
        self._rds_client: Any = None

    # =========================================================================
    # CONNECTION MANAGEMENT
    # =========================================================================

    async def connect(self) -> bool:
        """
        Connect to AWS and initialize boto3 clients.

        Requires explicit aws_access_key_id and aws_secret_access_key in
        credentials. No fallback to boto3's default credential chain (env vars,
        ~/.aws/credentials, instance profile) -- this would bypass multi-tenant
        credential isolation and the CredentialResolver chain.

        Returns:
            True if connection successful.

        Raises:
            ValueError: If required credentials are missing.
        """
        aws_access_key_id = self.credentials.get("aws_access_key_id")
        aws_secret_access_key = self.credentials.get("aws_secret_access_key")

        if not aws_access_key_id or not aws_secret_access_key:
            raise ValueError(
                "AWS credentials required: aws_access_key_id and aws_secret_access_key "
                "must be provided. MEHO is a multi-user server — environment/instance "
                "credentials are not used to enforce per-connector RBAC."
            )

        try:
            import boto3

            logger.info(f"Connecting to AWS region: {self._default_region}")

            self._session = boto3.Session(
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                region_name=self._default_region,
            )

            # Initialize clients in a thread to avoid blocking
            await asyncio.to_thread(self._initialize_clients)

            self._is_connected = True
            logger.info(f"Connected to AWS region: {self._default_region}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to AWS: {e}", exc_info=True)
            raise

    def _initialize_clients(self) -> None:
        """Initialize all boto3 service clients (runs in thread pool)."""
        self._ec2_client = self._session.client("ec2")
        self._cloudwatch_client = self._session.client("cloudwatch")
        self._ecs_client = self._session.client("ecs")
        self._eks_client = self._session.client("eks")
        self._s3_client = self._session.client("s3")
        self._lambda_client = self._session.client("lambda")
        self._rds_client = self._session.client("rds")

    async def disconnect(self) -> None:
        """Disconnect from AWS (cleanup clients)."""
        self._ec2_client = None
        self._cloudwatch_client = None
        self._ecs_client = None
        self._eks_client = None
        self._s3_client = None
        self._lambda_client = None
        self._rds_client = None
        self._session = None
        self._is_connected = False
        logger.info("Disconnected from AWS")

    async def test_connection(self) -> bool:
        """Test if connection is alive by calling STS GetCallerIdentity."""
        try:
            if not self._is_connected or not self._session:
                return False

            sts_client = self._session.client("sts")
            identity = await asyncio.to_thread(sts_client.get_caller_identity)
            logger.info(f"AWS connection test passed: account {identity.get('Account')}")
            return True

        except Exception as e:
            logger.error(f"AWS connection test failed: {e}", exc_info=True)
            return False

    # =========================================================================
    # CLIENT ACCESS
    # =========================================================================

    def _get_client(self, service_name: str, region: str | None = None) -> Any:
        """
        Get a boto3 client for the given service.

        Returns the cached default-region client if region is None or matches
        the default region. Otherwise creates an ad-hoc client for the
        specified region.

        Args:
            service_name: AWS service name (e.g., "ec2", "s3").
            region: Optional region override. If None, uses default region.

        Returns:
            boto3 service client.
        """
        if region is None or region == self._default_region:
            # Return cached default-region client
            client_attr = f"_{service_name}_client"
            client = getattr(self, client_attr, None)
            if client is not None:
                return client

        # Create ad-hoc client for non-default region
        return self._session.client(service_name, region_name=region or self._default_region)

    # =========================================================================
    # OPERATION EXECUTION
    # =========================================================================

    async def execute(self, operation_id: str, parameters: dict[str, Any]) -> OperationResult:
        """
        Execute an AWS operation.

        Args:
            operation_id: ID of the operation (e.g., "list_ec2_instances").
            parameters: Operation-specific parameters.

        Returns:
            OperationResult with success status and data/error.
        """
        if not self._is_connected:
            return OperationResult(
                success=False,
                error="Not connected to AWS",
                operation_id=operation_id,
            )

        start_time = time.time()

        try:
            # Find and execute the operation handler
            handler_name = f"_handle_{operation_id}"
            handler = getattr(self, handler_name, None)

            if handler is None:
                return OperationResult(
                    success=False,
                    error=f"Unknown operation: {operation_id}",
                    operation_id=operation_id,
                )

            result = await handler(parameters)
            duration_ms = (time.time() - start_time) * 1000

            logger.info(f"{operation_id}: completed in {duration_ms:.1f}ms")

            return OperationResult(
                success=True,
                data=result,
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            error_info = self._parse_aws_error(e, operation_id)
            logger.error(f"{operation_id} failed: {error_info['message']}", exc_info=True)
            return OperationResult(
                success=False,
                error=error_info["message"],
                error_code=error_info.get("code"),
                error_details=error_info.get("details"),
                operation_id=operation_id,
                duration_ms=duration_ms,
            )

    def _parse_aws_error(self, error: Exception, operation_id: str) -> dict[str, Any]:
        """
        Parse AWS API errors into structured, actionable error information.

        Handles botocore.exceptions.ClientError and maps common AWS error
        codes to MEHO-standard error codes.

        Args:
            error: The exception that was raised.
            operation_id: The operation that failed.

        Returns:
            Dict with 'message', 'code', and 'details' keys.
        """
        error_str = str(error)
        error_type = type(error).__name__

        # Try to handle botocore ClientError
        try:
            from botocore.exceptions import ClientError

            if isinstance(error, ClientError):
                response = error.response or {}
                error_info = response.get("Error", {})
                aws_error_code = error_info.get("Code", "")
                aws_error_message = error_info.get("Message", error_str)
                http_status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")

                # Map AWS error codes to MEHO-standard codes
                meho_code = self._map_aws_error_code(aws_error_code)

                return {
                    "code": meho_code,
                    "message": (
                        f"AWS {aws_error_code} for operation '{operation_id}': "
                        f"{aws_error_message}"
                    ),
                    "details": {
                        "error_type": error_type,
                        "aws_error_code": aws_error_code,
                        "http_status": http_status,
                        "operation": operation_id,
                        "raw_error": error_str[:500],
                    },
                }

        except ImportError:
            pass

        # Default error handling
        return {
            "code": "UNKNOWN",
            "message": error_str,
            "details": {
                "error_type": error_type,
                "operation": operation_id,
            },
        }

    @staticmethod
    def _map_aws_error_code(aws_code: str) -> str:
        """
        Map an AWS error code to a MEHO-standard error code.

        Args:
            aws_code: AWS-specific error code string.

        Returns:
            MEHO-standard error code.
        """
        permission_codes = {
            "AccessDeniedException",
            "UnauthorizedAccess",
            "AccessDenied",
            "UnauthorizedOperation",
        }
        not_found_codes = {
            "ResourceNotFoundException",
            "NotFoundException",
            "NoSuchEntity",
            "NoSuchBucket",
        }
        invalid_codes = {
            "ValidationException",
            "InvalidParameterValueException",
            "InvalidParameterValue",
            "InvalidParameterCombination",
        }
        throttle_codes = {
            "ThrottlingException",
            "Throttling",
            "TooManyRequestsException",
            "RequestLimitExceeded",
        }

        if aws_code in permission_codes:
            return "PERMISSION_DENIED"
        if aws_code in not_found_codes:
            return "NOT_FOUND"
        if aws_code in invalid_codes:
            return "INVALID_ARGUMENT"
        if aws_code in throttle_codes:
            return "QUOTA_EXCEEDED"

        return "UNKNOWN"

    # =========================================================================
    # OPERATION & TYPE DEFINITIONS
    # =========================================================================

    def get_operations(self) -> list[OperationDefinition]:
        """Get all AWS operation definitions."""
        return AWS_OPERATIONS

    def get_types(self) -> list[TypeDefinition]:
        """Get all AWS type definitions."""
        return AWS_TYPES
