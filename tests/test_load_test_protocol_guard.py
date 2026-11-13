"""不正なCLI契約が外部操作へ到達せず秘密値を出力しないことを確認する。"""

import os
import runpy
import sys
from typing import Never

import httpx
import pytest
from sqlalchemy import Engine

from quiz_app import controller_cli, fault_controller_cli
from quiz_app.load_test import cli as load_test_cli


@pytest.mark.parametrize(
    "main",
    [load_test_cli.main, controller_cli.main, fault_controller_cli.main],
)
def test_cli_invalid_contract_refuses_without_disclosing_input(
    main: object,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert callable(main)
    assert main(["private-argument-must-not-leak"]) == os.EX_CONFIG
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"status":"refused","code":"invalid_contract"}\n'


@pytest.mark.filterwarnings("ignore:.*found in sys.modules.*:RuntimeWarning")
@pytest.mark.parametrize(
    "module_name",
    [
        "quiz_app.load_test",
        "quiz_app.load_test.cli",
        "quiz_app.controller_cli",
        "quiz_app.fault_controller_cli",
    ],
)
def test_invalid_cli_refuses_before_connecting_or_creating_data(
    module_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected_side_effect(*_args: object, **_kwargs: object) -> Never:
        pytest.fail("旧正式試験の実行処理へ進んではいけません")

    monkeypatch.setattr(Engine, "connect", unexpected_side_effect)
    monkeypatch.setattr(httpx.Client, "send", unexpected_side_effect)
    monkeypatch.setattr(sys, "argv", [module_name, "private-argument-must-not-leak"])

    with pytest.raises(SystemExit) as stopped:
        runpy.run_module(module_name, run_name="__main__", alter_sys=True)

    assert stopped.value.code == os.EX_CONFIG
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"status":"refused","code":"invalid_contract"}\n'
