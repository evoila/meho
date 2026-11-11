# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The project check that runs before ``harbor.robot.delete`` deletes a robot.

The approval card of ``harbor.robot.delete`` says "a robot of project P". The
Harbor delete call takes only the robot id. So right before the delete, MEHO
reads the robot (``GET /api/v2.0/robots/{robot_id}``) and deletes it only when
it is a project-level robot whose every permission is on project P. A system
robot, or a robot of another project, is refused and no ``DELETE`` is sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["RobotOutsideProjectError", "check_robot_in_project"]


class RobotOutsideProjectError(ValueError):
    """The robot to delete is not a project robot of the named project."""


def check_robot_in_project(robot: Any, *, robot_id: int, project: str) -> None:
    """Raise :class:`RobotOutsideProjectError` unless *robot* belongs to *project* only.

    *robot* is the body of ``GET /api/v2.0/robots/{robot_id}`` (Harbor's
    ``Robot`` model). It must have ``level == "project"`` and at least one
    permission, and every permission must be ``kind == "project"`` with
    ``namespace == project``.
    """
    if _belongs_to(robot, project):
        return
    raise RobotOutsideProjectError(
        f"Robot {robot_id} is not a project robot of project {project!r}. "
        "MEHO did not delete it. Check the robot id and the project name."
    )


def _belongs_to(robot: Any, project: str) -> bool:
    if not isinstance(robot, Mapping) or robot.get("level") != "project":
        return False
    permissions = robot.get("permissions")
    if not isinstance(permissions, list) or not permissions:
        return False
    return all(
        isinstance(perm, Mapping)
        and perm.get("kind") == "project"
        and perm.get("namespace") == project
        for perm in permissions
    )
