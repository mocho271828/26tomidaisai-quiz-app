from typing import Annotated

from pydantic import StrictStr, StringConstraints

PUBLIC_LABEL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
PUBLIC_LABEL_PATTERN = r"^[A-Z]{3}$"

PublicLabel = Annotated[
    StrictStr,
    StringConstraints(pattern=PUBLIC_LABEL_PATTERN),
]


def normalize_public_label(value: object) -> object:
    """ASCII英字3文字だけを大文字へ正規化し、それ以外はvalidatorへ委ねる。"""

    if not isinstance(value, str):
        return value
    if len(value) != 3 or not value.isascii() or not value.isalpha():
        return value
    return value.upper()
