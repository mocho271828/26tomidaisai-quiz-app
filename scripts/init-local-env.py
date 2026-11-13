import base64
import os
import secrets
from pathlib import Path
from tempfile import NamedTemporaryFile


def main() -> None:
    """初回に限り、専用のバックアップ鍵を入れた0600の .env を作る。"""
    destination = Path(".env")
    template = Path(".env.example").read_text(encoding="utf-8")
    placeholder = "ROUND_BACKUP_ENCRYPTION_KEY=\n"
    if template.count(placeholder) != 1:
        raise SystemExit(".env.example のバックアップ鍵欄を確認してください")
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    with NamedTemporaryFile(mode="w", dir=".", prefix=".env.", encoding="utf-8") as temporary:
        temporary.write(template.replace(placeholder, f"ROUND_BACKUP_ENCRYPTION_KEY={key}\n"))
        temporary.flush()
        # 完成したファイルだけを公開し、並行実行や再実行で既存の鍵を上書きしない。
        try:
            os.link(temporary.name, destination)
        except FileExistsError:
            if not destination.is_file() or destination.is_symlink():
                raise SystemExit("既存の .env のファイル種別を確認してください") from None
            print("既存の .env を使用します。")
        else:
            print("この環境専用のバックアップ鍵を .env に保存しました。")


if __name__ == "__main__":
    main()
