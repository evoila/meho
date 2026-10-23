# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Contracts of the three git composites of the gh-rest connector.

Parameter schemas, response schemas and agent guidance (``llm_instructions``)
for ``gh.composite.git_ref_view``, ``gh.composite.git_tree_view`` and
``gh.composite.branch_commit_files``. The handlers live in :mod:`._git`.

Same conventions as :mod:`.schemas`: ``additionalProperties=False`` on every
parameter schema, a description on every field, and one example call with
neutral placeholders.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "BRANCH_COMMIT_FILES_LLM_INSTRUCTIONS",
    "BRANCH_COMMIT_FILES_PARAMETER_SCHEMA",
    "BRANCH_COMMIT_FILES_RESPONSE_SCHEMA",
    "GIT_REF_VIEW_LLM_INSTRUCTIONS",
    "GIT_REF_VIEW_PARAMETER_SCHEMA",
    "GIT_REF_VIEW_RESPONSE_SCHEMA",
    "GIT_TREE_VIEW_LLM_INSTRUCTIONS",
    "GIT_TREE_VIEW_PARAMETER_SCHEMA",
    "GIT_TREE_VIEW_RESPONSE_SCHEMA",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
]

#: Most files one ``branch_commit_files`` call may write.
MAX_FILES: Final[int] = 100
#: Largest single file, in bytes of UTF-8 text.
MAX_FILE_BYTES: Final[int] = 256 * 1024
#: Largest sum of all file contents, in bytes of UTF-8 text.
MAX_TOTAL_BYTES: Final[int] = 1024 * 1024

_OWNER: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "GitHub repository owner (user or organisation login).",
}
_REPO: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "GitHub repository name (without the owner prefix).",
}
_SHA: dict[str, Any] = {
    "type": ["string", "null"],
    "description": "A 40-character git object SHA-1.",
}


GIT_REF_VIEW_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["owner", "repo", "name"],
    "properties": {
        "owner": _OWNER,
        "repo": _REPO,
        "name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The branch or tag name, for example 'main' or 'v1.2.0'. MEHO builds "
                "the git ref ('heads/<name>' or 'tags/<name>') itself."
            ),
        },
        "kind": {
            "type": "string",
            "enum": ["branch", "tag"],
            "default": "branch",
            "description": "What the name is: a 'branch' (default) or a 'tag'.",
        },
    },
    "examples": [{"owner": "<owner>", "repo": "<repo>", "name": "main"}],
    "description": (
        "Parameters for gh.composite.git_ref_view: the commit a branch or tag points to."
    ),
}

GIT_REF_VIEW_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["found", "kind", "name", "ref", "sha", "object_type", "url"],
    "properties": {
        "found": {
            "type": "boolean",
            "description": "True when the branch or tag exists. False means GitHub answered 404.",
        },
        "kind": {"type": "string", "description": "Echo of the kind: 'branch' or 'tag'."},
        "name": {"type": "string", "description": "Echo of the branch or tag name."},
        "ref": {
            "type": "string",
            "description": "The git ref MEHO asked for, for example 'heads/main'.",
        },
        "sha": {
            **_SHA,
            "description": "SHA-1 of the object the ref points to. Null when not found.",
        },
        "object_type": {
            "type": ["string", "null"],
            "description": (
                "'commit' for a branch or a lightweight tag; 'tag' for an annotated tag "
                "object. Null when not found."
            ),
        },
        "url": {
            "type": ["string", "null"],
            "description": "API URL of the object the ref points to. Null when not found.",
        },
    },
}

GIT_REF_VIEW_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to learn which commit a branch or tag points to, for example before "
        "reading its file tree or before writing files to the branch."
    ),
    "parameter_hints": {
        "name": "Just the name ('main', 'release/1.2', 'v1.2.0'), never 'refs/heads/...'.",
        "kind": "'branch' (default) or 'tag'.",
    },
    "output_shape": (
        "{found, kind, name, ref, sha, object_type, url}. found=false means the "
        "branch or tag does not exist; sha is then null."
    ),
    "next_step": (
        "Pass sha to gh.composite.git_tree_view to list the files. An annotated tag "
        "(object_type='tag') points to a tag object, not a commit; git_tree_view still "
        "accepts the tag name."
    ),
}


GIT_TREE_VIEW_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["owner", "repo", "tree"],
    "properties": {
        "owner": _OWNER,
        "repo": _REPO,
        "tree": {
            "type": "string",
            "minLength": 1,
            "description": (
                "What to list: a tree SHA, a commit SHA, or a branch or tag name "
                "(for example 'main')."
            ),
        },
        "recursive": {
            "type": "boolean",
            "default": False,
            "description": (
                "False (default): one level only; folders appear as type 'tree'. "
                "True: every file in every folder, with its full path."
            ),
        },
    },
    "examples": [{"owner": "<owner>", "repo": "<repo>", "tree": "main", "recursive": True}],
    "description": "Parameters for gh.composite.git_tree_view: the files and folders of a tree.",
}

_TREE_ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One file or folder.",
    "properties": {
        "path": {"type": "string", "description": "Path inside the repository."},
        "type": {
            "type": "string",
            "description": "'blob' (file), 'tree' (folder) or 'commit' (submodule).",
        },
        "mode": {
            "type": "string",
            "description": "Git mode: 100644 file, 100755 executable, 040000 folder, 120000 link.",
        },
        "sha": {"type": "string", "description": "SHA-1 of the file or folder object."},
        "size": {
            "type": ["integer", "null"],
            "description": "Size in bytes for a file; null for a folder.",
        },
    },
}

GIT_TREE_VIEW_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["sha", "recursive", "truncated", "entry_count", "entries"],
    "properties": {
        "sha": {"type": "string", "description": "SHA-1 of the tree that was listed."},
        "recursive": {"type": "boolean", "description": "Echo of the recursive flag."},
        "truncated": {
            "type": "boolean",
            "description": (
                "True when GitHub cut the list (more than 100,000 entries or 7 MB). "
                "List one folder at a time then."
            ),
        },
        "entry_count": {"type": "integer", "description": "Number of entries returned."},
        "entries": {
            "type": "array",
            "description": "The files and folders, in GitHub's order.",
            "items": _TREE_ENTRY_SCHEMA,
        },
    },
}

GIT_TREE_VIEW_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to see which files and folders a branch, tag or commit holds, with "
        "each file's size and SHA-1."
    ),
    "parameter_hints": {
        "tree": "A branch name like 'main', a tag name, a commit SHA or a tree SHA.",
        "recursive": "Set true to get every file with its full path in one call.",
    },
    "output_shape": (
        "{sha, recursive, truncated, entry_count, entries: [{path, type, mode, sha, "
        "size}]}. A long list is behind a result handle; read it with result_query."
    ),
    "next_step": (
        "To open a folder from a non-recursive list, call again with tree=<the "
        "folder's sha>. If truncated is true, list folder by folder. To change files, "
        "call gh.composite.branch_commit_files."
    ),
    "result_scalars": {"keys": ["sha", "recursive", "truncated", "entry_count"]},
}


BRANCH_COMMIT_FILES_PARAMETER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["owner", "repo", "branch", "base_sha", "message", "files"],
    "properties": {
        "owner": _OWNER,
        "repo": _REPO,
        "branch": {
            "type": "string",
            "minLength": 1,
            "description": "The branch to commit to. It must exist; MEHO creates no branch.",
        },
        "base_sha": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The 40-character SHA-1 of the commit the branch points to, as read with "
                "gh.composite.git_ref_view. The new commit is built on it. If the branch "
                "is not at this commit any more, the call stops and changes nothing."
            ),
        },
        "message": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096,
            "description": "The commit message.",
        },
        "files": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_FILES,
            "description": (
                f"The text files to write (at most {MAX_FILES}). An existing file is "
                "replaced; a new file is created. Other files stay as they are."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "content"],
                "description": "One file.",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Path inside the repository, like 'docs/readme.md'. No leading "
                            "'/', no '.' or '..' parts."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "maxLength": MAX_FILE_BYTES,
                        "description": (
                            f"The full text of the file, UTF-8, at most {MAX_FILE_BYTES} "
                            f"bytes per file and {MAX_TOTAL_BYTES} bytes in total."
                        ),
                    },
                },
            },
        },
    },
    "examples": [
        {
            "owner": "<owner>",
            "repo": "<repo>",
            "branch": "<branch>",
            "base_sha": "<sha from git_ref_view>",
            "message": "docs: add the release notes",
            "files": [{"path": "docs/release-notes.md", "content": "# Release notes\n"}],
        }
    ],
    "description": (
        "Parameters for gh.composite.branch_commit_files: write text files to a branch "
        "as one new commit."
    ),
}

_WRITTEN_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One file of the commit.",
    "properties": {
        "path": {"type": "string", "description": "Path inside the repository."},
        "size": {"type": "integer", "description": "Size of the content in bytes (UTF-8)."},
        "sha256": {"type": "string", "description": "SHA-256 of the content, hex."},
    },
}

BRANCH_COMMIT_FILES_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "status",
        "reason",
        "branch",
        "base_sha",
        "tree_sha",
        "commit_sha",
        "html_url",
        "file_count",
        "files",
    ],
    "properties": {
        "status": {
            "type": "string",
            "enum": ["committed", "precondition_failed", "invalid_request"],
            "description": (
                "'committed': the branch now points to the new commit. "
                "'precondition_failed': the branch does not exist or is not at base_sha; "
                "nothing changed. 'invalid_request': a name, base_sha, file path or size "
                "is not allowed; nothing changed."
            ),
        },
        "reason": {
            "type": ["string", "null"],
            "description": "Why the call stopped. Null on 'committed'.",
        },
        "branch": {"type": "string", "description": "Echo of the branch name."},
        "base_sha": {
            **_SHA,
            "description": (
                "Echo of base_sha, the parent of the new commit. Null on invalid_request."
            ),
        },
        "tree_sha": {**_SHA, "description": "SHA-1 of the new tree. Null when not committed."},
        "commit_sha": {
            **_SHA,
            "description": "SHA-1 of the new commit. Null when not committed.",
        },
        "html_url": {
            "type": ["string", "null"],
            "description": "Web link to the new commit. Null when not committed.",
        },
        "file_count": {"type": "integer", "description": "Number of files in the call."},
        "files": {
            "type": "array",
            "description": "The files, each with its size and SHA-256 (never the content).",
            "items": _WRITTEN_FILE_SCHEMA,
        },
    },
}

BRANCH_COMMIT_FILES_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to create or replace one or more text files on an existing branch as one "
        "commit, for example to add a document or change a config file."
    ),
    "preconditions": (
        "Read the branch first with gh.composite.git_ref_view and pass its sha as "
        "base_sha. The branch must still be at base_sha when the call runs. The files "
        "are UTF-8 text, at most 256 KiB each and 1 MiB in total, at most 100 per "
        "call. The call waits for approval; the approver sees the branch, base_sha, "
        "the commit the branch is at now, and each file's path, size and SHA-256. "
        "MEHO never forces: if the branch is not at base_sha, the call stops and "
        "changes nothing."
    ),
    "parameter_hints": {
        "branch": "Just the branch name, like 'main' or 'feature/x'.",
        "base_sha": "The sha that gh.composite.git_ref_view returned for the branch.",
        "files": "Each entry needs the full new content of the file, not a diff.",
    },
    "output_shape": (
        "{status: committed|precondition_failed|invalid_request, reason, branch, "
        "base_sha, tree_sha, commit_sha, html_url, file_count, files: [{path, size, "
        "sha256}]}."
    ),
    "next_step": (
        "On committed, report commit_sha and html_url. On precondition_failed, read "
        "the branch again with gh.composite.git_ref_view, check the new commit, and "
        "call again with the new sha as base_sha and content based on it. On "
        "invalid_request, fix the name, sha, path or size named in reason."
    ),
    "result_scalars": {
        "keys": [
            "status",
            "reason",
            "branch",
            "base_sha",
            "tree_sha",
            "commit_sha",
            "html_url",
            "file_count",
        ]
    },
}
