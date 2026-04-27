# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS Connector Module.

Provides native Amazon Web Services integration using boto3.

Supported services:
- EC2: Instance management
- CloudWatch: Metrics and alarms
- ECS: Container service management
- EKS: Kubernetes cluster management
- S3: Object storage
- Lambda: Serverless functions
- RDS: Managed databases
"""

from meho_app.modules.connectors.aws.connector import AWSConnector
from meho_app.modules.connectors.aws.operations import AWS_OPERATIONS, AWS_OPERATIONS_VERSION
from meho_app.modules.connectors.aws.types import AWS_TYPES

__all__ = [
    "AWS_OPERATIONS",
    "AWS_OPERATIONS_VERSION",
    "AWS_TYPES",
    "AWSConnector",
]
