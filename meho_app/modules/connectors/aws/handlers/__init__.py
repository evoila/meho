# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
AWS Handler Mixins.

Exports all 8 service handler mixin classes for AWSConnector inheritance.
"""

from meho_app.modules.connectors.aws.handlers.cloudwatch_handlers import CloudWatchHandlerMixin
from meho_app.modules.connectors.aws.handlers.ec2_handlers import EC2HandlerMixin
from meho_app.modules.connectors.aws.handlers.ecs_handlers import ECSHandlerMixin
from meho_app.modules.connectors.aws.handlers.eks_handlers import EKSHandlerMixin
from meho_app.modules.connectors.aws.handlers.lambda_handlers import LambdaHandlerMixin
from meho_app.modules.connectors.aws.handlers.rds_handlers import RDSHandlerMixin
from meho_app.modules.connectors.aws.handlers.s3_handlers import S3HandlerMixin
from meho_app.modules.connectors.aws.handlers.vpc_handlers import VPCHandlerMixin

__all__ = [
    "CloudWatchHandlerMixin",
    "EC2HandlerMixin",
    "ECSHandlerMixin",
    "EKSHandlerMixin",
    "LambdaHandlerMixin",
    "RDSHandlerMixin",
    "S3HandlerMixin",
    "VPCHandlerMixin",
]
