# 一時ファイル: check-ok が matrix の失敗を拾うかの確認用 (#110)。確認後に revert する。
import sys


def test_fails_only_on_314() -> None:
    assert sys.version_info < (3, 14)
