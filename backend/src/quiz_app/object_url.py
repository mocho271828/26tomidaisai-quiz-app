from __future__ import annotations

import re
from ipaddress import ip_address
from urllib.parse import unquote, urlsplit

from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter

from quiz_app.config import is_local_service_host

MAX_OBJECT_URL_LENGTH = 512
MAX_OBJECT_HOST_LENGTH = 253
MAX_OBJECT_PATH_LENGTH = 512
DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
LEGACY_IPV4_LABEL_PATTERN = re.compile(r"^(?:0x[0-9a-f]+|0[0-7]*|\d+)$")
INVALID_PERCENT_ESCAPE_PATTERN = re.compile(r"%(?![0-9A-F]{2})")
PERCENT_ESCAPE_PATTERN = re.compile(r"%([0-9A-F]{2})")
URL_UNRESERVED_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
SPECIAL_USE_HOST_SUFFIXES = (".internal", ".arpa", ".onion", ".alt")
HTTP_URL_ADAPTER = TypeAdapter(AnyHttpUrl, config=ConfigDict(strict=True))


def validate_remote_object_host(host: str) -> str:
    """外部object originとして安全なcanonical FQDNだけを許可する。"""

    try:
        ip_address(host)
    except ValueError:
        is_ip_address = False
    else:
        is_ip_address = True
    if (
        len(host) > MAX_OBJECT_HOST_LENGTH
        or not host.isascii()
        or host != host.casefold()
        or host.endswith(".")
        or "." not in host
        or LEGACY_IPV4_LABEL_PATTERN.fullmatch(host.rsplit(".", 1)[-1]) is not None
        or is_ip_address
        or is_local_service_host(host)
        or host.endswith(SPECIAL_USE_HOST_SUFFIXES)
        or host == "r2.dev"
        or host.endswith(".r2.dev")
        or any(label.startswith("xn--") for label in host.split("."))
        or any(DNS_LABEL_PATTERN.fullmatch(label) is None for label in host.split("."))
    ):
        raise ValueError("object hostはcanonical remote FQDNで指定してください")
    return host


def validate_object_path(path: str) -> str:
    """query等とdot traversalを含まないcanonical absolute object pathを検証する。"""

    if (
        len(path) > MAX_OBJECT_PATH_LENGTH
        or not path.isascii()
        or not path.startswith("/")
        or path == "/"
        or "\\" in path
        or "?" in path
        or "#" in path
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in path)
        or INVALID_PERCENT_ESCAPE_PATTERN.search(path) is not None
        or any(
            chr(int(match.group(1), 16)) in URL_UNRESERVED_CHARACTERS
            for match in PERCENT_ESCAPE_PATTERN.finditer(path)
        )
        or _has_dot_path_segment(path)
    ):
        raise ValueError("object pathはcanonical absolute object pathで指定してください")
    return path


def validate_canonical_remote_https_object_url(object_url: str) -> str:
    """credentialやportを持たないremote HTTPS object URLを検証する。"""

    parsed_url = HTTP_URL_ADAPTER.validate_python(object_url)
    split_url = urlsplit(object_url)
    host = split_url.hostname
    if host is None:
        raise ValueError("object URLにはhostが必要です")
    validate_remote_object_host(host)
    validate_object_path(split_url.path)
    if (
        len(object_url) > MAX_OBJECT_URL_LENGTH
        or not object_url.isascii()
        or parsed_url.scheme != "https"
        or parsed_url.host != host
        or split_url.port is not None
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.path != split_url.path
        or parsed_url.query is not None
        or parsed_url.fragment is not None
        or str(parsed_url) != object_url
    ):
        raise ValueError("object URLはcanonical remote HTTPS URLで指定してください")
    return object_url


def _has_dot_path_segment(path: str) -> bool:
    """percent decodeを重ねても`.`/`..` segmentにならないことを確認する。"""

    candidate = path
    for _ in range(len(path) + 1):
        if any(segment in {".", ".."} for segment in candidate.replace("\\", "/").split("/")):
            return True
        try:
            decoded = unquote(candidate, errors="strict")
        except UnicodeDecodeError:
            return True
        if decoded == candidate:
            return False
        candidate = decoded
    return True
