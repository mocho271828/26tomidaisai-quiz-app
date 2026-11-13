"""配置入力のエラーを、値を含まない固定の設定案内へ変換する。"""

from pydantic import BaseModel, ConfigDict, ValidationError


class ConfigurationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    variables: tuple[str, ...]
    requirement: str


_REQUIREMENTS = {
    ("expected_event_id",): ConfigurationIssue(
        variables=("AWS_REVIEW_EXPECTED_EVENT_ID",),
        requirement="現在の運営stateで確認したEvent UUIDを手動workflowへ指定してください。",
    ),
    ("expected_event_version",): ConfigurationIssue(
        variables=("AWS_REVIEW_EXPECTED_EVENT_VERSION",),
        requirement="現在の運営stateで確認したEvent versionを正整数で指定してください。",
    ),
    ("confirmation",): ConfigurationIssue(
        variables=("AWS_REVIEW_RESET_CONFIRMATION",),
        requirement="削除範囲を確認し、手動workflowの確認欄へRESET EVENTと入力してください。",
    ),
    ("deployment_mode",): ConfigurationIssue(
        variables=("AWS_REVIEW_DEPLOYMENT_MODE",),
        requirement="手動workflowで既存環境のupdate、または新規環境のcreateを選択してください。",
    ),
    ("app_instance_type",): ConfigurationIssue(
        variables=("AWS_APP_INSTANCE_TYPE",),
        requirement="手動workflowでc7i.xlargeまたはt3.mediumを選択してください。",
    ),
    ("public_entrypoint",): ConfigurationIssue(
        variables=("AWS_REVIEW_PUBLIC_ENTRYPOINT",),
        requirement="手動workflowのpublic_entrypointでcustom-domainまたはcloudfrontを"
        "選択してください。",
    ),
    ("review_domain_name",): ConfigurationIssue(
        variables=("AWS_REVIEW_DOMAIN_NAME",),
        requirement="ACM証明書の対象となる小文字のDNSホスト名を設定してください。",
    ),
    ("review_certificate_arn",): ConfigurationIssue(
        variables=("AWS_REVIEW_CERTIFICATE_ARN",),
        requirement="Environment secretsにAWS_ACCOUNT_IDと同一accountのus-east-1の"
        "ACM証明書ARNを設定してください。公開ドメインを含むISSUED証明書が必要です。",
    ),
    ("account_id",): ConfigurationIssue(
        variables=("AWS_ACCOUNT_ID",),
        requirement="Environment secretsに半角数字12桁を設定してください。",
    ),
    ("demo_id",): ConfigurationIssue(
        variables=("AWS_REVIEW_ID",),
        requirement="Environment secretsに小文字英数字と単語間のハイフンで21文字以内のIDを"
        "設定し、IAMのDemoIdと一致させてください。",
    ),
    ("app_az",): ConfigurationIssue(
        variables=("AWS_APP_AZ",),
        requirement="Environment secretsにap-northeast-1aの形式の東京リージョンAZ名を"
        "設定してください。AZ IDではなくAZ名を使います。",
    ),
    ("secondary_az",): ConfigurationIssue(
        variables=("AWS_SECONDARY_AZ",),
        requirement="Environment secretsにap-northeast-1cの形式の東京リージョンAZ名を"
        "設定してください。AZ IDではなくAZ名を使います。",
    ),
    ("ubuntu_ami_id",): ConfigurationIssue(
        variables=("AWS_UBUNTU_AMI_ID",),
        requirement="Environment secretsにami-に続く小文字16進数17桁のAMI IDを設定してください。",
    ),
    ("database_engine_version",): ConfigurationIssue(
        variables=("AWS_DATABASE_ENGINE_VERSION",),
        requirement="Environment secretsに16.6のような16.<数字>形式のPostgreSQL versionを"
        "設定してください。利用可能なversionを対象accountで確認してください。",
    ),
    ("source_commit",): ConfigurationIssue(
        variables=("GITHUB_SHA",),
        requirement="GitHub Actionsが設定する40桁の小文字16進commit SHAが必要です。"
        "mainからworkflowを新規実行してください。",
    ),
    ("vite_event_start_at",): ConfigurationIssue(
        variables=("VITE_EVENT_START_AT",),
        requirement="Environment variablesに2026-09-30T18:00:00+09:00のような"
        "タイムゾーン付きISO日時を設定するか、空欄にしてください。",
    ),
    **{
        (field,): ConfigurationIssue(
            variables=(variable,),
            requirement="Environment variablesに認証情報・空白を含まない2048文字以内の"
            "外部HTTPS URLを設定するか、空欄にしてください。"
            "クイズの公開ドメイン自身へのURLは使えません。",
        )
        for field, variable in (
            ("vite_survey_url", "VITE_SURVEY_URL"),
            ("vite_home_url", "VITE_HOME_URL"),
        )
    },
    # 現在のDeploymentInputのmodel validatorはAZの相違だけを検証する。
    (): ConfigurationIssue(
        variables=("AWS_APP_AZ", "AWS_SECONDARY_AZ"),
        requirement="Environment secretsに互いに異なる東京リージョンAZ名を設定してください。",
    ),
}
_UNKNOWN_ISSUE = ConfigurationIssue(
    variables=(),
    requirement="aws-reviewの設定をdocs/GITHUB_ACTIONS_AWS.mdの入力条件と照合してください。",
)


def configuration_issues(error: ValidationError) -> tuple[ConfigurationIssue, ...]:
    """CLIの設定検証に限り、既知の項目と固定案内だけを返す。"""
    issues: list[ConfigurationIssue] = []
    for detail in error.errors(include_url=False, include_context=False, include_input=False):
        # msgにも入力値が入り得るため、locを完全一致で照合し固定値だけを採用する。
        issue = _REQUIREMENTS.get(detail["loc"], _UNKNOWN_ISSUE)
        if issue not in issues:
            issues.append(issue)
    return tuple(issues)
