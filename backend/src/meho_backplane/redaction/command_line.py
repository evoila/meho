# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hide secret values on a command line or in a shell script, word by word.

Used by the approval previews that show a command or a script to the
approver: ``k8s.job.create`` and ``k8s.cronjob.run`` (a container's
``command`` and ``args``) and ``linux.script.run`` (the script text). The
words stay, so the approver still reads what runs. Hidden are:

* the value of a ``NAME=VALUE`` or ``--name=value`` whose name holds a secret
  word, also inside a longer name (``PGPASSWORD=...``,
  ``AWS_SECRET_ACCESS_KEY=...``, ``--password=...``);
* the argument after a secret flag (``--password x``, ``--token x``,
  ``-p x``);
* the password in ``user:password`` after ``-u`` or ``--user``;
* the password in a URL that carries a user and password;
* tokens with a known vendor prefix (``ghp_...``).

:func:`hide_shell_script` runs all of these over a whole shell script. There
``-p`` is a secret flag only for programs that take a password with it
(``sshpass``, ``mysql``, ``mariadb``), and a password glued to it
(``mysql -pVALUE``) is hidden too.
"""

from __future__ import annotations

import re
from typing import Final

from meho_backplane.broadcast.events import is_secret_param_name

__all__ = [
    "HIDDEN",
    "hide_command_text",
    "hide_flag_value",
    "hide_in_text",
    "hide_shell_script",
    "reads_shell_script",
]

#: Put in place of a value the approver must not see.
HIDDEN: Final[str] = "***REDACTED***"

#: Words that make a variable or flag name secret-shaped on a command line,
#: also inside a longer name (``PGPASSWORD``, ``AWS_SECRET_ACCESS_KEY``).
_COMMAND_SECRET_WORDS = ("PASS", "PWD", "SECRET", "TOKEN", "KEY", "CREDENTIAL", "AUTH")

#: Flags whose next word is ``user:password`` (``curl -u``, ``--user``).
_USER_FLAGS = frozenset({"u", "user"})


#: The characters that end a shell word when they are not quoted or escaped:
#: the shell's operators (``;``, ``&&``, ``|``, ``2>&1``, ``( )``).
_OPERATOR_CHARS = "|&;()<>"

#: Regex for one shell word, read the way a POSIX shell reads it. A word is
#: bare text up to whitespace or an operator, escaped characters (``\\ ``,
#: ``\\;`` and ``\\"`` are part of the word, not an end of it), single-quoted
#: parts and double-quoted parts in which ``\\"`` does not close the quote, in
#: any mix. So ``"PGPASSWORD=x"suffix`` is one word and ``PGPASSWORD=x;id``
#: is two, as for the shell. A quote that is never closed runs to the end of
#: the string, as in a shell.
_SHELL_WORD = (
    rf"""(?:[^\s'"\\{_OPERATOR_CHARS}]+|\\(?:[\s\S]|\Z)|'[^']*(?:'|\Z)|"""
    r""""(?:[^"\\]|\\[\s\S])*(?:"|\Z))+"""
)

#: One token of a shell command string: whitespace, a run of operator
#: characters, or a shell word in which a quoted or escaped part may hold
#: spaces (``--password 'my phrase'``, ``--password my\\ phrase``). Joined
#: back, the tokens give the string as it was.
_SHELL_TOKEN = re.compile(rf"""\s+|[{_OPERATOR_CHARS}]+|{_SHELL_WORD}|\S+""")

#: One token of a direct argument (a list item no shell reads): whitespace
#: or a run of other characters. Quotes and operators are plain text here.
_ARGUMENT_TOKEN = re.compile(r"\s+|\S+")

#: A shell word that is one quoted part and nothing else: a shell string, as
#: the one after ``sh -c``, or a quoted value.
_QUOTED_STRING = re.compile(r"'[^']*'|" r'"(?:[^"\\]|\\[\s\S])*"')

#: The ``NAME=`` of a ``NAME=VALUE``, also as ``--name=``, where a word or a
#: quoted part starts, or after another ``=`` (``--env=PGPASSWORD=x``).
_ASSIGNMENT_NAME = re.compile(r"(?<![A-Za-z0-9_.-])(?P<name>-{0,2}[A-Za-z_][A-Za-z0-9_.-]*)=")

#: The password in a URL that carries a user and password before the ``@``.
_URL_PASSWORD = re.compile(r"(?P<head>[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]*:)[^\s/@]+@")

#: Tokens with a known vendor prefix (GitHub, GitLab, Slack, AWS key id).
_KNOWN_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b"
)

#: A command-line flag: ``-u``, ``--password``.
_FLAG = re.compile(r"^--?(?P<name>[A-Za-z][A-Za-z0-9_-]*)$")

#: The ``-c`` of a shell, alone or in a short option group (``-ec``, ``-lc``,
#: ``-euc``): the argument after it is a shell command string.
_SHELL_SCRIPT_OPTION = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")


#: How ``-p`` is read in the current command: not a secret flag, a secret
#: flag, or a secret flag that may also carry its value glued (``-pVALUE``).
_P_NONE: Final[str] = "none"
_P_FLAG: Final[str] = "flag"
_P_GLUED: Final[str] = "glued"


def reads_shell_script(word: str) -> bool:
    """Whether the argument after *word* is a shell command string (``sh -c``, ``bash -lc``)."""
    return _SHELL_SCRIPT_OPTION.match(word) is not None


def _secret_shaped_name(name: str) -> bool:
    """True when a variable or flag name on a command line can name a secret."""
    upper = name.upper()
    return is_secret_param_name(name) or any(word in upper for word in _COMMAND_SECRET_WORDS)


def _hide_user_password(value: str) -> str:
    """``user:password`` with the password hidden; other text as it is."""
    user, sep, _ = value.partition(":")
    return f"{user}:{HIDDEN}" if sep else value


def hide_in_text(text: str) -> str:
    """Hide the secret values with a shape of their own in one command-line string.

    The password in a URL and tokens with a known vendor prefix are found
    wherever they are. The other values are hidden word by word, by
    :func:`hide_command_text`.
    """
    text = _URL_PASSWORD.sub(lambda match: f"{match['head']}{HIDDEN}@", text)
    return _KNOWN_TOKEN.sub(HIDDEN, text)


def _secret_assignment(word: str) -> re.Match[str] | None:
    """The first ``NAME=`` in a shell word whose value must be hidden, or ``None``.

    That is a secret-shaped name, or ``-u`` / ``--user``, whose value holds
    ``user:password``.
    """
    for match in _ASSIGNMENT_NAME.finditer(word):
        name = match["name"]
        if _secret_shaped_name(name) or name.lstrip("-") in _USER_FLAGS:
            return match
    return None


def _hide_assignment(word: str, match: re.Match[str]) -> str:
    """*word* with all that follows its ``NAME=`` at *match* hidden.

    *word* is the whole value span: one shell word in a shell string
    (``"PGPASSWORD=x"suffix`` is one word, and the shell gives the variable
    ``xsuffix``, so ``suffix`` is hidden too), or the rest of a direct
    argument. No quote is read. A quote right before the name is closed
    after the marker, so the line still reads as a command line. After
    ``-u`` or ``--user`` only the password of ``user:password`` is hidden.
    """
    rest = word[match.end() :]
    hidden = _hide_user_password(rest) if match["name"].lstrip("-") in _USER_FLAGS else HIDDEN
    quote = word[match.start() - 1] if match.start() > 0 else ""
    closing = quote if quote in "'\"" else ""
    return f"{word[: match.end()]}{hidden}{closing}"


def hide_flag_value(pending: str, value: str) -> str:
    """The value that follows a secret flag, hidden the way the flag asks."""
    return HIDDEN if pending == "secret" else _hide_user_password(value)


def hide_command_text(
    text: str,
    pending: str | None,
    *,
    shell: bool,
    p_programs: re.Pattern[str] | None = None,
) -> tuple[str, str | None]:
    """One command-line string with its secret values hidden.

    *pending* is the secret flag that still waits for its value when the
    string starts (``--password`` ended the item before). Returned with the
    text is the flag that waits at its end, for the next item.

    With *shell* the text is a shell command string, the one after ``-c``
    (or ``-ec``, ``-lc``). It is split into shell words, in which a quoted or escaped part may hold
    spaces. For each word, in this order:

    * An unquoted shell operator (``;``, ``&&``, ``|``) ends the command
      before it: a flag that still waits for its value gets none, and the
      next word is a new command, shown.
    * After a secret flag, the whole word is the value: hidden.
    * A word that is one quoted part and starts with a secret-named
      assignment (``"PGPASSWORD=a b"``) is that assignment: hidden whole.
      Not after ``-c``: there it is a shell string.
    * Any other word that is one quoted part is read on its own: as a
      shell command string after ``-c`` (``sh -c "PGPASSWORD=x; id"``
      keeps ``id``), else as a direct argument.
    * A word that holds a secret-named assignment anywhere (``PGPASSWORD=x``,
      ``"PGPASSWORD=x"suffix``, ``--env=PGPASSWORD=x``) is hidden from the
      ``NAME=`` to its end.
    * A secret flag makes the next word its value.

    Without *shell* the text is a direct argument: one argument of the
    program, which no shell splits. Its words are found at whitespace only,
    quotes and operators are plain text, and the first secret value found
    (after a secret flag, or after a secret-named ``NAME=``) runs to the end
    of the argument: ``PGPASSWORD=x;tail`` and ``--password x y`` hide all
    that follows the marker.

    *p_programs* narrows the short flag ``-p``. Without it, ``-p`` is a
    secret flag after any program. With it, ``-p`` is a secret flag only in
    a command that has a word matching *p_programs* (``sshpass``,
    ``mysql``), and there a value glued to it (``-pVALUE``) is hidden too.
    So ``mkdir -p /tmp/x`` stays as it is. With *p_programs* a new line
    also ends a command, as a shell operator does.
    """
    words = (_SHELL_TOKEN if shell else _ARGUMENT_TOKEN).findall(text)
    previous = ""
    p_rule = _P_FLAG if p_programs is None else _P_NONE
    for index, word in enumerate(words):
        if word.isspace():
            if p_programs is not None and "\n" in word:
                pending = None
                p_rule = _P_NONE
            continue
        if shell and word[0] in _OPERATOR_CHARS:
            pending = None
            previous = ""
            p_rule = _P_FLAG if p_programs is None else _P_NONE
            continue
        previous, prior = word, previous
        value = word if shell else "".join(words[index:])
        hidden, pending = _hide_word(
            word,
            value,
            prior=prior,
            pending=pending,
            shell=shell,
            p_rule=p_rule,
            p_programs=p_programs,
        )
        if p_programs is not None and p_programs.fullmatch(word.rsplit("/", 1)[-1]):
            p_rule = _P_GLUED
        if hidden is None:
            continue
        words[index] = hidden
        if not shell:
            return "".join(words[: index + 1]), pending
    return "".join(words), pending


def _hide_word(
    word: str,
    value: str,
    *,
    prior: str,
    pending: str | None,
    shell: bool,
    p_rule: str,
    p_programs: re.Pattern[str] | None,
) -> tuple[str | None, str | None]:
    """One word of a command line: its hidden form, or ``None`` when it is shown.

    *value* is the span a secret in *word* hides: the word itself in a shell
    string, the rest of the argument in a direct one. *prior* is the word
    before it. *p_rule* says how ``-p`` is read in this command. Returned
    with the hidden form is the secret flag that waits for the next word.
    The rules are those of :func:`hide_command_text`.
    """
    if pending is not None and not word.startswith("-"):
        return hide_flag_value(pending, value), None
    assignment = _secret_assignment(word)
    script = reads_shell_script(prior)
    quoted_assignment = assignment is not None and assignment.start() == 1 and not script
    if shell and not quoted_assignment and _QUOTED_STRING.fullmatch(word):
        inner, waiting = hide_command_text(word[1:-1], None, shell=script, p_programs=p_programs)
        return f"{word[0]}{inner}{word[0]}", waiting
    if assignment is not None:
        return _hide_assignment(value, assignment), None
    if p_rule == _P_GLUED and word.startswith("-p") and len(word) > 2:
        return f"-p{HIDDEN}", None
    flag = _FLAG.match(word)
    if flag is None:
        return None, None
    name = flag["name"]
    if name in _USER_FLAGS:
        return None, "user"
    p_secret = name == "p" and p_rule != _P_NONE
    return None, "secret" if p_secret or _secret_shaped_name(name) else None


#: The programs whose ``-p`` takes a password in a shell script, also glued
#: to it (``sshpass -pVALUE``, ``mysql -pVALUE``, ``mysqldump``, ``mariadb``).
_SCRIPT_P_PROGRAMS = re.compile(r"sshpass|mysql[A-Za-z0-9_-]*|mariadb[A-Za-z0-9_-]*")


def hide_shell_script(script: str) -> str:
    """A whole shell script with the secret values listed in the module docstring hidden.

    The script is read as one shell command string: new lines, loops and
    commands stay as they are. ``-p`` is a secret flag only for the programs
    of :data:`_SCRIPT_P_PROGRAMS`, so ``mkdir -p`` and ``ssh -p 22`` stay.
    """
    text, _ = hide_command_text(
        hide_in_text(script), None, shell=True, p_programs=_SCRIPT_P_PROGRAMS
    )
    return text
