from __future__ import annotations

import re
import shlex
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from quiz_app import controller_cli, db_verifier_cli, evidence_handoff_cli

HANDOFF = Path(__file__).resolve().parents[1] / "docs" / "LOAD_TEST_EVIDENCE_HANDOFF.md"
CLI_PARSERS = {
    "quiz_app.controller_cli": controller_cli._build_parser,
    "quiz_app.evidence_handoff_cli": evidence_handoff_cli._build_parser,
    "quiz_app.db_verifier_cli": db_verifier_cli._build_parser,
}


def _shell_commands(markdown: str) -> list[list[str]]:
    return [
        shlex.split(line)
        for block in re.findall(r"```(?:sh|bash)\n(.*?)\n```", markdown, re.S)
        for line in block.replace("\\\n", "").splitlines()
        if line.strip()
    ]


@pytest.mark.parametrize(
    "retired_contract",
    (
        "single_event_protocol_unsupported",
        "--real-device-evidence",
        "seal-real-device",
        "30冪等record",
        "50 audit",
        "自動公開audit",
    ),
)
def test_retired_contracts_are_absent_from_current_handoff(retired_contract: str) -> None:
    # 現行方針は廃止契約を本文に残さず、過去の判断をGit履歴で管理する。
    current = HANDOFF.read_text(encoding="utf-8")
    assert retired_contract not in current


def test_handoff_describes_manual_publication_and_current_verifier_counts() -> None:
    current = HANDOFF.read_text(encoding="utf-8")

    assert "prepare → schedule → publish → wait-results → advance" in current
    assert "`schedule` 成功: `status=waiting,next_action=publish`" in current
    assert "`publish` 成功: `status=waiting,next_action=wait-results`" in current
    assert "`aggregate_round_results`をaccount/keyなしのsystem actor" in current
    assert "`publish_round_results`はoperator accountと公開用の冪等keyを要求" in current
    # 集計と公開を分けた現行verifierの最終期待値を文書にも要求する。
    counts = re.search(r"最終期待は \*\*(\d+) audit・(\d+)冪等record\*\*", current)
    assert counts is not None
    assert tuple(map(int, counts.groups())) == (60, 40)


def test_handoff_links_to_current_contracts_and_existing_targets() -> None:
    current = HANDOFF.read_text(encoding="utf-8")
    links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", current)
    assert {"LOAD_TEST_FORMAL_V3.md", "AWS_LOAD_TEST_AUTOMATION.md"} <= {
        urlsplit(link).path for link in links
    }
    assert "AWS_LOAD_TEST_AUTOMATION.md#証跡の回収" in links
    assert "#controller-statusと検証request" in links
    assert "#per-roundfinal照合" in links

    for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", HANDOFF.read_text(encoding="utf-8")):
        target = urlsplit(link)
        assert not target.scheme and not target.netloc, link
        path = HANDOFF.parent / unquote(target.path) if target.path else HANDOFF
        assert path.is_file(), link
        if target.fragment:
            # 対象文書のATX見出しだけを扱い、Markdown全般の解析は行わない。
            headings = re.findall(r"^#{1,6} +(.+)$", path.read_text(encoding="utf-8"), re.M)
            anchors = {
                re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-") for heading in headings
            }
            assert unquote(target.fragment) in anchors, link


def test_current_shell_examples_match_cli_parsers_without_execution() -> None:
    current = HANDOFF.read_text(encoding="utf-8")
    parsed_commands: set[tuple[str, str | None]] = set()
    for tokens in _shell_commands(current):
        if "-m" not in tokens:
            continue
        module_index = tokens.index("-m") + 1
        module = tokens[module_index]
        if module not in CLI_PARSERS:
            continue
        arguments = tokens[module_index + 1 :]
        if ">" in arguments:
            arguments = arguments[: arguments.index(">")]
        # parserのみを呼び、CLIの実行・shell展開・AWS/DB接続はしない。
        parsed = CLI_PARSERS[module]().parse_args(arguments)
        action = getattr(parsed, "command", getattr(parsed, "action", None))
        parsed_commands.add((module, action))
        if action == "build-db-input":
            assert parsed.mode == "per-round"
            arguments[arguments.index("--mode") + 1] = "final"
            assert CLI_PARSERS[module]().parse_args(arguments).mode == "final"

    assert parsed_commands == {
        ("quiz_app.controller_cli", "wait-results"),
        ("quiz_app.evidence_handoff_cli", "build-db-input"),
        ("quiz_app.evidence_handoff_cli", "build-lock-input"),
        ("quiz_app.db_verifier_cli", None),
    }


def test_collector_example_matches_authoritative_aws_runbook() -> None:
    current = HANDOFF.read_text(encoding="utf-8")
    automation = (HANDOFF.parent / "AWS_LOAD_TEST_AUTOMATION.md").read_text(encoding="utf-8")
    handoff_commands = [
        tokens for tokens in _shell_commands(current) if "quiz_app.load_test_collect" in tokens
    ]
    authoritative_commands = [
        tokens for tokens in _shell_commands(automation) if "quiz_app.load_test_collect" in tokens
    ]
    assert len(handoff_commands) == 1
    assert handoff_commands == authoritative_commands
