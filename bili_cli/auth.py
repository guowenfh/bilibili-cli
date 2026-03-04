"""Authentication for Bilibili.

Strategy:
1. Try loading saved credential from ~/.bilibili-cli/credential.json
2. Try extracting cookies from local browsers via browser-cookie3
3. Fallback: QR code login via bilibili-api-python + terminal display
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

import qrcode
from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents
from bilibili_api.utils.network import Credential

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".bilibili-cli"
CREDENTIAL_FILE = CONFIG_DIR / "credential.json"

# Required cookies for a valid Bilibili session
REQUIRED_COOKIES = {"SESSDATA"}


AuthMode = Literal["optional", "read", "write"]


def get_credential(mode: AuthMode = "read") -> Credential | None:
    """Try auth methods in order and return credential according to mode.

    - optional: only load saved credential (no network validation, no browser scan)
    - read: prefer validated credential; if validation is indeterminate (network),
      return saved/browser credential as best effort
    - write: same as read, but require bili_jct capability
    """
    require_write = mode == "write"

    # 1. Saved credential file
    cred = _load_saved_credential()
    if cred:
        if mode == "optional":
            return cred
        validation = _validate_credential(cred, require_write=require_write)
        if validation is True:
            logger.info("Loaded valid credential from %s", CREDENTIAL_FILE)
            return cred
        if validation is None:
            logger.warning("Credential validation failed due to network; using saved credential as best effort")
            return cred
        if validation is False:
            logger.warning("Saved credential is expired, clearing")
            clear_credential()

    if mode == "optional":
        return None

    # 2. Browser cookie extraction
    cred = _extract_browser_credential()
    if cred:
        validation = _validate_credential(cred, require_write=require_write)
        if validation is True:
            logger.info("Extracted valid credential from local browser")
            save_credential(cred)
            return cred
        if validation is None:
            logger.warning("Skipping browser credential validation due to network; using best effort")
            return cred
        if validation is False:
            logger.warning("Browser cookies are expired/invalid")

    return None


def _validate_credential(cred: Credential, require_write: bool = False) -> bool | None:
    """Check if a credential is valid.

    Returns:
      - True: credential validated by API
      - False: credential confirmed invalid or missing required fields
      - None: validation is indeterminate due to network/runtime issues
    """
    from bilibili_api import user
    from bilibili_api.exceptions import NetworkException

    if not getattr(cred, "sessdata", ""):
        return False
    if require_write and not getattr(cred, "bili_jct", ""):
        return False

    async def _check():
        try:
            await user.get_self_info(cred)
            return True
        except NetworkException:
            return None
        except Exception:
            return False

    try:
        return asyncio.run(_check())
    except Exception:
        return None


def _load_saved_credential() -> Credential | None:
    """Load credential from saved file."""
    if not CREDENTIAL_FILE.exists():
        return None

    try:
        data = json.loads(CREDENTIAL_FILE.read_text())
        sessdata = data.get("sessdata", "")
        if not sessdata:
            return None
        return Credential(
            sessdata=sessdata,
            bili_jct=data.get("bili_jct", ""),
            ac_time_value=data.get("ac_time_value", ""),
        )
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Failed to load saved credential: %s", e)
        return None


def _extract_browser_credential() -> Credential | None:
    """Extract Bilibili cookies from local browsers using browser-cookie3.

    Runs extraction in a subprocess with timeout to avoid hanging
    when the browser is running (Chrome DB lock issue).
    """
    extract_script = '''
import json, sys
try:
    import browser_cookie3 as bc3
except ImportError:
    print(json.dumps({"error": "not_installed"}))
    sys.exit(0)

browsers = [
    ("Chrome", bc3.chrome),
    ("Firefox", bc3.firefox),
    ("Edge", bc3.edge),
    ("Brave", bc3.brave),
]

for name, loader in browsers:
    try:
        cj = loader(domain_name=".bilibili.com")
        cookies = {c.name: c.value for c in cj if "bilibili.com" in (c.domain or "")}
        if "SESSDATA" in cookies:
            print(json.dumps({"browser": name, "cookies": cookies}))
            sys.exit(0)
    except Exception:
        pass

print(json.dumps({"error": "no_cookies"}))
'''

    try:
        result = subprocess.run(
            [sys.executable, "-c", extract_script],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode != 0:
            logger.debug("Cookie extraction subprocess failed: %s", result.stderr)
            return None

        output = result.stdout.strip()
        if not output:
            logger.debug("Cookie extraction returned empty output")
            return None

        data = json.loads(output)

        if "error" in data:
            if data["error"] == "not_installed":
                logger.debug("browser-cookie3 not installed, skipping")
            else:
                logger.debug("No valid Bilibili cookies found in any browser")
            return None

        cookies = data["cookies"]
        browser_name = data["browser"]
        if not REQUIRED_COOKIES.issubset(cookies):
            logger.debug("Browser cookies missing required keys: %s", REQUIRED_COOKIES)
            return None
        logger.info(
            "Found valid cookies in %s (%d cookies)", browser_name, len(cookies)
        )

        return Credential(
            sessdata=cookies.get("SESSDATA", ""),
            bili_jct=cookies.get("bili_jct", ""),
            ac_time_value=cookies.get("ac_time_value", ""),
        )

    except subprocess.TimeoutExpired:
        logger.warning(
            "Cookie extraction timed out (browser may be running). "
            "Try closing your browser or use `bili login`."
        )
        return None
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Cookie extraction parse error: %s", e)
        return None


def save_credential(credential: Credential):
    """Save credential to config file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    data = {
        "sessdata": credential.sessdata,
        "bili_jct": credential.bili_jct,
        "ac_time_value": credential.ac_time_value or "",
    }
    CREDENTIAL_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    CREDENTIAL_FILE.chmod(0o600)  # Owner-only read/write
    logger.info("Credential saved to %s", CREDENTIAL_FILE)


def clear_credential():
    """Remove saved credential file."""
    if CREDENTIAL_FILE.exists():
        CREDENTIAL_FILE.unlink()
        logger.info("Credential removed: %s", CREDENTIAL_FILE)


def _render_compact_qr(data: str) -> str:
    """Render a compact QR code using Unicode half-block characters.

    Uses ▀, ▄, █, and space to encode two vertical modules per character row,
    reducing the QR code height by half compared to full-block rendering.
    Each module is 1 character wide (vs 2 in qrcode-terminal), so total area
    is ~25% of the original.
    """
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    # Add 1-module quiet zone
    size = len(matrix)
    padded = [[False] * (size + 2)]
    for row in matrix:
        padded.append([False] + list(row) + [False])
    padded.append([False] * (size + 2))
    matrix = padded
    rows = len(matrix)

    # Check terminal width and warn if too narrow
    term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    qr_width = len(matrix[0])
    if qr_width > term_cols:
        return (
            f"⚠️  终端宽度 ({term_cols}) 不足以显示二维码 ({qr_width})，"
            "请放大终端窗口或缩小字体后重试。"
        )

    lines: list[str] = []
    # Process two rows at a time using half-block characters
    # top=black, bottom=black → █ (full block)
    # top=black, bottom=white → ▀ (upper half)
    # top=white, bottom=black → ▄ (lower half)
    # top=white, bottom=white → ' ' (space)
    for y in range(0, rows, 2):
        line = ""
        top_row = matrix[y]
        bottom_row = matrix[y + 1] if y + 1 < rows else [False] * len(top_row)
        for x in range(len(top_row)):
            top = top_row[x]
            bottom = bottom_row[x]
            if top and bottom:
                line += "█"
            elif top and not bottom:
                line += "▀"
            elif not top and bottom:
                line += "▄"
            else:
                line += " "
        lines.append(line)
    return "\n".join(lines)


async def qr_login() -> Credential:
    """QR code login via terminal.

    Displays a QR code in the terminal, polls until login completes,
    then saves and returns the credential.
    """
    login = QrCodeLogin()
    await login.generate_qrcode()

    # Extract QR link and render compact version
    qr_link = login._QrCodeLogin__qr_link  # access private attr for custom rendering

    # Display QR code in terminal
    print("\n📱 请使用 Bilibili App 扫描以下二维码登录:\n")
    print(_render_compact_qr(qr_link))
    print("\n⭐ 扫码后请在手机上确认登录...")

    # Poll login state
    while True:
        state = await login.check_state()

        if state == QrCodeLoginEvents.DONE:
            credential = login.get_credential()
            save_credential(credential)
            print("\n✅ 登录成功！凭证已保存")
            return credential

        elif state == QrCodeLoginEvents.TIMEOUT:
            raise RuntimeError("二维码已过期，请重试")

        elif state == QrCodeLoginEvents.CONF:
            print("  📲 已扫码，请在手机上确认...")

        await asyncio.sleep(2)
