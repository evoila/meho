# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS Operation Definitions.

Aggregates all operation definitions from category-specific modules.
"""

from meho_app.modules.connectors.aws.operations.cloudwatch import CLOUDWATCH_OPERATIONS
from meho_app.modules.connectors.aws.operations.ec2 import EC2_OPERATIONS
from meho_app.modules.connectors.aws.operations.ecs import ECS_OPERATIONS
from meho_app.modules.connectors.aws.operations.eks import EKS_OPERATIONS
from meho_app.modules.connectors.aws.operations.lambda_ops import LAMBDA_OPERATIONS
from meho_app.modules.connectors.aws.operations.rds import RDS_OPERATIONS
from meho_app.modules.connectors.aws.operations.s3 import S3_OPERATIONS
from meho_app.modules.connectors.aws.operations.vpc import VPC_OPERATIONS

AWS_OPERATIONS_VERSION = "2026.03.27.1"

AWS_OPERATIONS = (
    CLOUDWATCH_OPERATIONS
    + EC2_OPERATIONS
    + ECS_OPERATIONS
    + EKS_OPERATIONS
    + S3_OPERATIONS
    + LAMBDA_OPERATIONS
    + RDS_OPERATIONS
    + VPC_OPERATIONS
)

__all__ = [
    "AWS_OPERATIONS",
    "AWS_OPERATIONS_VERSION",
    "CLOUDWATCH_OPERATIONS",
    "EC2_OPERATIONS",
    "ECS_OPERATIONS",
    "EKS_OPERATIONS",
    "LAMBDA_OPERATIONS",
    "RDS_OPERATIONS",
    "S3_OPERATIONS",
    "VPC_OPERATIONS",
]
