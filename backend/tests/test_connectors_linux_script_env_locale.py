# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``linux.script.run`` passes each value byte for byte, in any host locale.

The test builds the multibyte locales ``zh_CN.GBK`` and ``zh_CN.GB18030``
with ``localedef`` into a temp folder (``LOCPATH``) and runs the real
script wrapper with bash under them:

* an ``env`` value that ends with a non-ASCII character reaches the script
  with the same bytes as the approval card shows;
* a ``secret_env`` value from Vault reaches the script with the same bytes
  (without the white space around it, as every value read from Vault).

The unit shards in CI install the locale data (step "Install the GBK locale
data") and set ``MEHO_REQUIRE_GBK_LOCALE=1``, so there the test fails
instead of skipping when the data is missing.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane.connectors._shared.vault_creds import strip_credential_value
from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.connectors.linux.ops_write import build_script_run_wrapper, linux_script_run
from tests.test_connectors_linux_script_card_env import _park, _proposed_effect
from tests.test_connectors_linux_write import (
    _SECRET_ENV_FIELD,
    _SECRET_ENV_REF,
    _TARGET,
    _proc,
    _required_settings_env,  # noqa: F401 -- autouse fixture
    linux_write_registered,  # noqa: F401 -- fixture
)

_LOCALES = ("zh_CN.GBK", "zh_CN.GB18030")
_I18N = Path("/usr/share/i18n")
_REQUIRED = os.environ.get("MEHO_REQUIRE_GBK_LOCALE") == "1"
_SCRIPT = "#!/bin/bash\nset -eu\ntar -cf /tmp/out.tar /etc/app\n"
_PRINT_ENV_HEX = (
    "import os, sys\nsys.stdout.write(os.environb[os.environ['MEHO_VAR'].encode()].hex())\n"
)

# Each value ends with a non-ASCII character. In GBK and GB18030 the last
# byte of these characters is a lead byte, so a pattern that follows the
# locale would read it and the next ASCII byte as one character.
_VALUES = ["中", "ends in ä", "ends in 中\n", "中\n\n", "x中x中"]


def _locale_data_missing() -> str | None:
    if shutil.which("bash") is None:
        return "bash is not installed"
    if shutil.which("localedef") is None:
        return "localedef is not installed"
    for path in ("locales/zh_CN", "charmaps/GBK.gz", "charmaps/GB18030.gz"):
        if not (_I18N / path).is_file():
            return f"{_I18N / path} is missing (package locales)"
    return None


@pytest.fixture(scope="module")
def locpath(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A folder with the built GBK and GB18030 locales, for ``LOCPATH``."""
    missing = _locale_data_missing()
    if missing is not None:
        if _REQUIRED:
            pytest.fail(f"MEHO_REQUIRE_GBK_LOCALE=1 but {missing}")
        pytest.skip(f"{missing}; set MEHO_REQUIRE_GBK_LOCALE=1 to fail instead of skip")
    folder = tmp_path_factory.mktemp("locales")
    for name in _LOCALES:
        charmap = name.split(".", 1)[1]
        subprocess.run(
            ["localedef", "-i", "zh_CN", "-f", charmap, str(folder / name)],
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert (folder / name).is_dir(), f"localedef did not build {name}"
    return folder


def _locale_env(locpath: Path, locale: str) -> dict[str, str]:
    return {**os.environ, "LOCPATH": str(locpath), "LC_ALL": locale}


def _assert_locale_is_multibyte(locpath: Path, locale: str) -> None:
    """The locale is active: a locale-following pattern keeps a mark after ``中``."""
    proc = subprocess.run(
        ["bash", "-c", 'v="$(printf %s "$1"; printf x)"; printf %s "${v%x}"', "_", "中"],
        capture_output=True,
        env=_locale_env(locpath, locale),
        timeout=30,
        check=True,
    )
    assert proc.stdout == "中x".encode(), (locale, proc.stdout, proc.stderr)


def _bytes_the_script_gets(locpath: Path, locale: str, wrapper: str, name: str) -> bytes:
    proc = subprocess.run(
        ["bash", "-c", wrapper],
        capture_output=True,
        env={**_locale_env(locpath, locale), "MEHO_VAR": name},
        timeout=30,
        check=True,
    )
    return bytes.fromhex(proc.stdout.decode("ascii"))


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", _LOCALES)
async def test_env_value_reaches_the_script_as_on_the_card_in_a_multibyte_locale(
    linux_write_registered: None,  # noqa: F811 -- fixture
    locpath: Path,
    locale: str,
) -> None:
    await asyncio.to_thread(_assert_locale_is_multibyte, locpath, locale)
    for value in _VALUES:
        env = {"APP_CONFIG": value}
        request_id = await _park({"script": _SCRIPT, "env": env})
        card_value = (await _proposed_effect(request_id))["preview"]["env"]["APP_CONFIG"]
        assert card_value == value
        wrapper = build_script_run_wrapper(
            sys.executable, _PRINT_ENV_HEX, arguments=None, working_directory=None, env=env
        )
        got = await asyncio.to_thread(
            _bytes_the_script_gets, locpath, locale, wrapper, "APP_CONFIG"
        )
        assert got == card_value.encode("utf-8"), (locale, value)


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", _LOCALES)
async def test_secret_env_value_reaches_the_script_byte_for_byte_in_a_multibyte_locale(
    locpath: Path,
    locale: str,
) -> None:
    await asyncio.to_thread(_assert_locale_is_multibyte, locpath, locale)
    for value in _VALUES:
        connector = LinuxSshConnector()
        connector._resolve_secret = AsyncMock(  # type: ignore[method-assign]
            return_value={_SECRET_ENV_FIELD: value}
        )

        async def _run_locally(_target: Any, cmd: str, **_kwargs: Any) -> Any:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["bash", "-c", cmd],
                capture_output=True,
                env={**_locale_env(locpath, locale), "MEHO_VAR": "APP_DB_CRED"},
                timeout=30,
                check=False,
            )
            return _proc(
                stdout=proc.stdout.decode("ascii", "replace"),
                stderr=proc.stderr.decode("utf-8", "replace"),
                exit_status=proc.returncode,
            )

        connector._run_command = _run_locally  # type: ignore[method-assign]
        result = await linux_script_run(
            connector,
            _TARGET,
            {
                "script": _PRINT_ENV_HEX,
                "interpreter": sys.executable,
                "secret_env": {"APP_DB_CRED": _SECRET_ENV_REF},
            },
        )
        assert result["exit_code"] == 0, result
        # The run takes the Vault value without the white space around it
        # (strip_credential_value), as for every credential read from Vault.
        expected = strip_credential_value(value).encode("utf-8")
        assert bytes.fromhex("".join(result["stdout"])) == expected, (locale, value)
