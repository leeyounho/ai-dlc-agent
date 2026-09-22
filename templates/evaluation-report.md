# Agent 운영 도입 평가 {{evaluation_id}}

판정: {{pass_fail_or_missing_evidence}} · 평가 시각: {{evaluated_at}} · 평가 정책: {{adopted_policy_revision}}

## 평가 대상과 범위

{{agent_commit_package_prompt_tool_and_config_digests}}

{{model_provider_versions_and_purpose_routes}}

{{repositories_task_types_workflow_policies_os_toolchains_deployment_scope}}

{{unverified_or_excluded_scope_and_reasons}}

## 평가 자료와 실행 조건

{{suite_revision_independent_group_count_holdout_status_repetitions_and_budgets}}

{{actual_environment_or_test_double_distinction}}

## 도입 관문

| 관문 | 채택 기준 | 실제 결과·분모 | 판정 | 근거 |
| --- | --- | --- | --- | --- |
| 중대 오류·필수 시나리오 | {{critical_threshold}} | {{critical_observations}} | {{critical_status}} | {{critical_evidence}} |
| 업무 품질·반복 안정성 | {{quality_threshold}} | {{success_counts_rates_and_interval}} | {{quality_status}} | {{quality_evidence}} |
| 독립 인수·사람 검토 | {{review_threshold}} | {{review_results}} | {{review_status}} | {{review_evidence}} |
| 실용성·자원·처리 시간 | {{usefulness_threshold}} | {{human_time_and_resource_results}} | {{usefulness_status}} | {{usefulness_evidence}} |
| 실제 전체 흐름·환경 | {{environment_threshold}} | {{scope_matrix_results}} | {{environment_status}} | {{environment_evidence}} |

## 구간별 결과

{{per_repository_task_type_difficulty_and_policy_results}}

{{complete_vs_clarify_block_recover_separate_results}}

## 실패와 사람의 추가 개입

{{all_failed_cases_assisted_outcomes_timeouts_and_exclusions_with_reasons}}

{{first_attempt_vs_budgeted_success_retries_and_repeat_variance}}

## 기준 방식·다른 모델 구성과 비교

{{paired_success_table_and_human_time_comparison}}

{{latency_resource_usage_and_missing_model_version_information}}

## 운영 적용 판단

{{eligible_scope_or_reasons_to_hold}}

{{remaining_evidence_required_and_regression_plan}}

이 보고서는 기록된 버전·정책·모델·환경·업무 범위에만 적용합니다. 미측정 항목은 합격으로 처리하지 않으며 평가 합격 자체로 배포를 실행하지 않습니다.
