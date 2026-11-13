# Binance 成交修复：最终 pytest 结果

实际执行：`./.venv/bin/python -m pytest -q --junitxml=/tmp/mirrorquant-fill-recovery-pytest.xml`（backend 目录）。

**591 passed, 1 warning in 44.23s**；失败 0，错误 0，跳过 0。新增测试 32 项，既有测试 559 项。

本文件从最终成功运行的 JUnit XML 生成。既有的 Starlette/httpx 弃用警告 1 条；pytest-asyncio 同时输出既有 fixture loop scope 提示。

含地址字面量的 10 个既有参数化测试，其参数显示为 SHA-256 前 12 位标识；函数名与实际结果保留，避免将地址或凭据写入报告及 commit。

## 本次新增测试

| 实例名称 | 结果 |
| --- | --- |
| `tests/test_bapi_fill_recovery.py::test_idless_same_millisecond_fills_are_both_accounted_without_regression` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_repeated_page_sync_preserves_exact_store_and_accounting` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_idless_key_uses_canonical_quantity_price_and_ignores_unverified_fields` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_real_order_id_cumulative_regression_still_fails_atomically` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_invalid_fill_quantity_fails_closed_without_partial_store[NaN-None]` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_invalid_fill_quantity_fails_closed_without_partial_store[NaN-test-order]` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_invalid_fill_quantity_fails_closed_without_partial_store[Infinity-None]` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_invalid_fill_quantity_fails_closed_without_partial_store[Infinity-test-order]` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_invalid_fill_quantity_fails_closed_without_partial_store[-Infinity-None]` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_invalid_fill_quantity_fails_closed_without_partial_store[-Infinity-test-order]` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_invalid_fill_quantity_fails_closed_without_partial_store[0-None]` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_invalid_fill_quantity_fails_closed_without_partial_store[0-test-order]` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_invalid_fill_quantity_fails_closed_without_partial_store[-1-None]` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_invalid_fill_quantity_fails_closed_without_partial_store[-1-test-order]` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_invalid_stored_quantity_fails_closed[NaN]` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_invalid_stored_quantity_fails_closed[Infinity]` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_invalid_stored_quantity_fails_closed[0]` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_invalid_stored_quantity_fails_closed[-1]` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_conflicting_individual_identity_fails_closed` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_rebuild_migration_is_idempotent_and_rollback_restores_dirty_state` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_rebuild_failure_keeps_backup_and_database_unchanged` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_rebuild_apply_and_rollback_refuse_newer_source_data` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_rebuild_blocks_inflight_old_sync_publication` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_truncated_closed_history_fails_closed` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_full_sweep_cannot_accept_duplicate_pages_as_complete` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_stale_failure_notifies_once_and_preserves_original_generated_at` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_consecutive_failures_have_error_log_outbox_and_degraded_readiness` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_recovery_resets_failure_incident_and_rearms_notification` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_restored_order_id_retains_legacy_progress_alias_on_repeated_sync` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_apply_accepts_only_failure_metadata_changes_since_prepare` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_duplicate_failure_attempt_does_not_increment_count_or_notify_twice` | PASSED |
| `tests/test_bapi_fill_recovery.py::test_explicit_rollback_of_newer_data_backs_it_up_before_restore` | PASSED |

## 全部既有测试

| 实例名称 | 结果 |
| --- | --- |
| `tests/test_account_update_nulls.py::test_account_update_rejects_explicit_null[name]` | PASSED |
| `tests/test_account_update_nulls.py::test_account_update_rejects_explicit_null[environment]` | PASSED |
| `tests/test_account_update_nulls.py::test_account_update_rejects_explicit_null[apiKey]` | PASSED |
| `tests/test_account_update_nulls.py::test_account_update_rejects_explicit_null[secretKey]` | PASSED |
| `tests/test_account_update_nulls.py::test_account_update_rejects_explicit_null[isEnabled]` | PASSED |
| `tests/test_account_update_nulls.py::test_account_update_rejects_explicit_null[marginMode]` | PASSED |
| `tests/test_account_update_nulls.py::test_account_update_rejects_explicit_null[positionMode]` | PASSED |
| `tests/test_account_update_nulls.py::test_account_partial_update_keeps_omitted_values` | PASSED |
| `tests/test_account_update_nulls.py::test_account_nullable_proxy_can_be_explicitly_cleared` | PASSED |
| `tests/test_adapter_remediations.py::test_order_mutation_is_never_retried_after_timeout[BinanceAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_order_mutation_is_never_retried_after_timeout[BitgetAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_order_mutation_is_never_retried_after_timeout[BybitAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_order_mutation_is_never_retried_after_timeout[OKXAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_idempotent_reads_may_retry[BinanceAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_idempotent_reads_may_retry[BitgetAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_idempotent_reads_may_retry[BybitAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_idempotent_reads_may_retry[OKXAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_only_documented_absent_orders_return_none[BinanceAdapter--2013]` | PASSED |
| `tests/test_adapter_remediations.py::test_only_documented_absent_orders_return_none[BinanceAdapter--2011]` | PASSED |
| `tests/test_adapter_remediations.py::test_only_documented_absent_orders_return_none[BitgetAdapter-40768]` | PASSED |
| `tests/test_adapter_remediations.py::test_only_documented_absent_orders_return_none[BitgetAdapter-40762]` | PASSED |
| `tests/test_adapter_remediations.py::test_only_documented_absent_orders_return_none[BybitAdapter-110001]` | PASSED |
| `tests/test_adapter_remediations.py::test_only_documented_absent_orders_return_none[BybitAdapter-170213]` | PASSED |
| `tests/test_adapter_remediations.py::test_only_documented_absent_orders_return_none[OKXAdapter-51603]` | PASSED |
| `tests/test_adapter_remediations.py::test_only_documented_absent_orders_return_none[OKXAdapter-51001]` | PASSED |
| `tests/test_adapter_remediations.py::test_zero_fill_is_preserved[BinanceAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_zero_fill_is_preserved[BitgetAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_zero_fill_is_preserved[BybitAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_zero_fill_is_preserved[OKXAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_bybit_queries_history_after_empty_realtime` | PASSED |
| `tests/test_adapter_remediations.py::test_binance_equity_includes_isolated_losses` | PASSED |
| `tests/test_adapter_remediations.py::test_okx_has_no_total_account_equity_fallback_and_normalizes_net_side` | PASSED |
| `tests/test_adapter_remediations.py::test_bybit_missing_or_zero_available_balance_never_uses_equity[None]` | PASSED |
| `tests/test_adapter_remediations.py::test_bybit_missing_or_zero_available_balance_never_uses_equity[]` | PASSED |
| `tests/test_adapter_remediations.py::test_bybit_missing_or_zero_available_balance_never_uses_equity[0_0]` | PASSED |
| `tests/test_adapter_remediations.py::test_bybit_missing_or_zero_available_balance_never_uses_equity[0_1]` | PASSED |
| `tests/test_adapter_remediations.py::test_bybit_multi_collateral_margin_is_not_qualified_usdt_capital` | PASSED |
| `tests/test_adapter_remediations.py::test_bitget_zero_available_is_preserved` | PASSED |
| `tests/test_adapter_remediations.py::test_bybit_position_pagination_and_repeated_cursor_failure` | PASSED |
| `tests/test_adapter_remediations.py::test_okx_only_complete_linear_base_unit_contracts_are_admitted` | PASSED |
| `tests/test_adapter_remediations.py::test_bitget_price_end_step_defines_actual_tick` | PASSED |
| `tests/test_adapter_remediations.py::test_symbol_mapping_cannot_change_economic_underlying[BinanceAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_symbol_mapping_cannot_change_economic_underlying[BitgetAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_symbol_mapping_cannot_change_economic_underlying[BybitAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_symbol_mapping_cannot_change_economic_underlying[OKXAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_market_specific_and_final_notional_filters` | PASSED |
| `tests/test_adapter_remediations.py::test_book_depth_is_normalized_to_supported_wire_values[BinanceAdapter-1-5]` | PASSED |
| `tests/test_adapter_remediations.py::test_book_depth_is_normalized_to_supported_wire_values[BinanceAdapter-2-5]` | PASSED |
| `tests/test_adapter_remediations.py::test_book_depth_is_normalized_to_supported_wire_values[BitgetAdapter-2-5]` | PASSED |
| `tests/test_adapter_remediations.py::test_unqualified_modes_block_entries_before_mutation[BinanceAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_unqualified_modes_block_entries_before_mutation[BitgetAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_unqualified_modes_block_entries_before_mutation[BybitAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_unqualified_modes_block_entries_before_mutation[OKXAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_okx_59000_does_not_report_leverage_success` | PASSED |
| `tests/test_adapter_remediations.py::test_binance_requires_matching_leverage_readback` | PASSED |
| `tests/test_adapter_remediations.py::test_binance_projected_notional_selects_actual_tier` | PASSED |
| `tests/test_adapter_remediations.py::test_binance_protection_uses_algo_order_endpoint` | PASSED |
| `tests/test_adapter_remediations.py::test_bitget_oneway_protection_uses_buy_sell_hold_side` | PASSED |
| `tests/test_adapter_remediations.py::test_clock_calibration_rejects_uncertain_sample_and_retains_previous_offset` | PASSED |
| `tests/test_adapter_remediations.py::test_pending_inventory_includes_conditional_orders[BinanceAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_pending_inventory_includes_conditional_orders[BitgetAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_pending_inventory_includes_conditional_orders[BybitAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_pending_inventory_includes_conditional_orders[OKXAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_bybit_malformed_inventory_does_not_mean_empty` | PASSED |
| `tests/test_adapter_remediations.py::test_bitget_truncated_inventory_fails_closed` | PASSED |
| `tests/test_adapter_remediations.py::test_error_responses_never_replay_mutations_and_share_rate_cooldown[429-BinanceAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_error_responses_never_replay_mutations_and_share_rate_cooldown[429-BitgetAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_error_responses_never_replay_mutations_and_share_rate_cooldown[429-BybitAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_error_responses_never_replay_mutations_and_share_rate_cooldown[429-OKXAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_error_responses_never_replay_mutations_and_share_rate_cooldown[500-BinanceAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_error_responses_never_replay_mutations_and_share_rate_cooldown[500-BitgetAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_error_responses_never_replay_mutations_and_share_rate_cooldown[500-BybitAdapter]` | PASSED |
| `tests/test_adapter_remediations.py::test_error_responses_never_replay_mutations_and_share_rate_cooldown[500-OKXAdapter]` | PASSED |
| `tests/test_adapters.py::test_okx_symbol_resolution` | PASSED |
| `tests/test_adapters.py::test_binance_symbol_resolution` | PASSED |
| `tests/test_adapters.py::test_bybit_symbol_resolution_and_signature` | PASSED |
| `tests/test_adapters.py::test_bitget_symbol_resolution_and_signature` | PASSED |
| `tests/test_adapters.py::test_adapter_factory_registration` | PASSED |
| `tests/test_analysis_remediations.py::test_rsi_extremes_and_flat_series[values0-50.0]` | PASSED |
| `tests/test_analysis_remediations.py::test_rsi_extremes_and_flat_series[values1-100.0]` | PASSED |
| `tests/test_analysis_remediations.py::test_rsi_extremes_and_flat_series[values2-0.0]` | PASSED |
| `tests/test_analysis_remediations.py::test_rsi_extremes_and_flat_series[values3-None]` | PASSED |
| `tests/test_analysis_remediations.py::test_zero_rsi_is_preserved_in_both_summaries` | PASSED |
| `tests/test_analysis_remediations.py::test_fresh_analysis_reports_source_timestamp_not_wall_clock` | PASSED |
| `tests/test_analysis_remediations.py::test_either_stale_timeframe_marks_analysis_stale[1h]` | PASSED |
| `tests/test_analysis_remediations.py::test_either_stale_timeframe_marks_analysis_stale[4h]` | PASSED |
| `tests/test_analysis_remediations.py::test_staleness_bound_is_configurable` | PASSED |
| `tests/test_analysis_remediations.py::test_missing_source_timestamp_never_reports_fresh_analysis` | PASSED |
| `tests/test_analysis_remediations.py::test_future_close_time_does_not_advance_source_timestamp` | PASSED |
| `tests/test_analysis_remediations.py::test_fresh_cached_klines_avoid_network` | PASSED |
| `tests/test_analysis_remediations.py::test_stale_data_refetches_even_with_recent_cache_write` | PASSED |
| `tests/test_analysis_remediations.py::test_failed_refetch_exposes_stale_cache_timestamp` | PASSED |
| `tests/test_analysis_remediations.py::test_successful_fetch_of_old_data_is_still_stale` | PASSED |
| `tests/test_api_endpoints.py::test_api_full_workflow` | PASSED |
| `tests/test_api_endpoints.py::test_login_brute_force_lockout_and_captcha` | PASSED |
| `tests/test_auth_medium_remediations.py::test_untrusted_peer_cannot_spoof_client_ip[X-Forwarded-For]` | PASSED |
| `tests/test_auth_medium_remediations.py::test_untrusted_peer_cannot_spoof_client_ip[X-Real-IP]` | PASSED |
| `tests/test_auth_medium_remediations.py::test_untrusted_peer_cannot_spoof_client_ip[CF-Connecting-IP]` | PASSED |
| `tests/test_auth_medium_remediations.py::test_trusted_proxy_chain_stops_at_first_untrusted_hop` | PASSED |
| `tests/test_auth_medium_remediations.py::test_trusted_proxy_accepts_validated_single_address[X-Real-IP]` | PASSED |
| `tests/test_auth_medium_remediations.py::test_trusted_proxy_accepts_validated_single_address[CF-Connecting-IP]` | PASSED |
| `tests/test_auth_medium_remediations.py::test_malformed_forwarded_chain_preserves_trusted_boundary[参数标识:1ebd5fdd088f]` | PASSED |
| `tests/test_auth_medium_remediations.py::test_malformed_forwarded_chain_preserves_trusted_boundary[参数标识:73beef0435fa]` | PASSED |
| `tests/test_auth_medium_remediations.py::test_trusted_proxy_cidrs_parse_and_validate` | PASSED |
| `tests/test_auth_medium_remediations.py::test_captcha_is_single_use_and_keeps_answer_digest_server_side` | PASSED |
| `tests/test_auth_medium_remediations.py::test_incorrect_captcha_attempt_consumes_nonce` | PASSED |
| `tests/test_auth_medium_remediations.py::test_captcha_expires_on_monotonic_deadline` | PASSED |
| `tests/test_auth_medium_remediations.py::test_captcha_nonce_cache_is_bounded_and_consumption_is_atomic` | PASSED |
| `tests/test_auth_medium_remediations.py::test_captcha_replay_returns_400_even_after_successful_login` | PASSED |
| `tests/test_auth_medium_remediations.py::test_password_helpers_await_worker_executor` | PASSED |
| `tests/test_auth_medium_remediations.py::test_concurrent_password_requests_bound_argon2_work` | PASSED |
| `tests/test_auth_medium_remediations.py::test_password_admission_bounds_submitted_work` | PASSED |
| `tests/test_auth_medium_remediations.py::test_auth_password_operations_run_outside_event_loop` | PASSED |
| `tests/test_bapi_collector.py::test_order_dedup_key_invariance` | PASSED |
| `tests/test_bapi_collector.py::test_direction_and_batching` | PASSED |
| `tests/test_bapi_collector.py::test_reconstruct_trade_cycles_from_reference_flow` | PASSED |
| `tests/test_engine_proxy_integration.py::test_unassigned_engine_account_uses_healthy_configured_proxy[sync_account-False]` | PASSED |
| `tests/test_engine_proxy_integration.py::test_unassigned_engine_account_uses_healthy_configured_proxy[sync_account-True]` | PASSED |
| `tests/test_engine_proxy_integration.py::test_unassigned_engine_account_uses_healthy_configured_proxy[risk_tick-False]` | PASSED |
| `tests/test_engine_proxy_integration.py::test_unassigned_engine_account_uses_healthy_configured_proxy[risk_tick-True]` | PASSED |
| `tests/test_engine_proxy_integration.py::test_unassigned_engine_account_uses_healthy_configured_proxy[router-False]` | PASSED |
| `tests/test_engine_proxy_integration.py::test_unassigned_engine_account_uses_healthy_configured_proxy[router-True]` | PASSED |
| `tests/test_engine_proxy_integration.py::test_lead_sync_reports_unavailable_proxy_without_contacting_upstream` | PASSED |
| `tests/test_engine_proxy_integration.py::test_binance_lead_failover_never_attempts_direct[False]` | PASSED |
| `tests/test_engine_proxy_integration.py::test_binance_lead_failover_never_attempts_direct[True]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_default_exchange_lists` | PASSED |
| `tests/test_exchange_egress_policy.py::test_exchange_lists_accept_environment_overrides[ Binance , BYBIT, - OKX, Bitget, ]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_exchange_lists_accept_environment_overrides[["Binance", "BYBIT"]-["OKX", "Bitget"]]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_empty_exchange_lists_accept_environment_overrides` | PASSED |
| `tests/test_exchange_egress_policy.py::test_allowlisted_exchange_builds_direct_client_despite_global_false[okx]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_allowlisted_exchange_builds_direct_client_despite_global_false[bybit]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_allowlisted_exchange_builds_direct_client_despite_global_false[bitget]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_allowlisted_exchange_builds_direct_client_despite_global_false[ OKX ]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_direct_is_rejected_before_transport_creation[False-False]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_direct_is_rejected_before_transport_creation[False-True]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_direct_is_rejected_before_transport_creation[True-False]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_direct_is_rejected_before_transport_creation[True-True]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_no_healthy_proxy_fails_closed[False-missing]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_no_healthy_proxy_fails_closed[False-disabled]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_no_healthy_proxy_fails_closed[False-unhealthy]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_no_healthy_proxy_fails_closed[True-missing]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_no_healthy_proxy_fails_closed[True-disabled]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_no_healthy_proxy_fails_closed[True-unhealthy]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_ordered_routes_never_include_direct[False]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_ordered_routes_never_include_direct[True]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_okx_prefers_configured_healthy_proxy_and_appends_direct` | PASSED |
| `tests/test_exchange_egress_policy.py::test_direct_exchange_unassigned_proxy_orders_direct_only_with_healthy_pool[okx]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_direct_exchange_unassigned_proxy_orders_direct_only_with_healthy_pool[bybit]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_direct_exchange_unassigned_proxy_orders_direct_only_with_healthy_pool[bitget]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_direct_exchange_unassigned_proxy_orders_direct_only_with_healthy_pool[ OKX ]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_okx_unassigned_proxy_selects_direct_with_healthy_pool` | PASSED |
| `tests/test_exchange_egress_policy.py::test_okx_explicit_proxy_excludes_pool_favourite[1]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_okx_explicit_proxy_excludes_pool_favourite[2]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_okx_unhealthy_assigned_proxy_does_not_borrow_healthy_pool` | PASSED |
| `tests/test_exchange_egress_policy.py::test_okx_missing_assigned_proxy_does_not_borrow_healthy_pool` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_unassigned_proxy_uses_healthy_pool_without_direct` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_unassigned_proxy_with_empty_pool_fails_closed` | PASSED |
| `tests/test_exchange_egress_policy.py::test_okx_explicit_none_builds_direct_client_despite_global_false` | PASSED |
| `tests/test_exchange_egress_policy.py::test_okx_unavailable_configured_proxy_falls_back_to_direct[missing]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_okx_unavailable_configured_proxy_falls_back_to_direct[disabled]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_okx_unavailable_configured_proxy_falls_back_to_direct[unhealthy]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_unknown_exchange_preserves_global_policy[False-None]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_unknown_exchange_preserves_global_policy[False-unknown]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_unknown_exchange_preserves_global_policy[True-None]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_unknown_exchange_preserves_global_policy[True-unknown]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_adapters_pass_inherited_exchange_identity[OKXAdapter]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_adapters_pass_inherited_exchange_identity[BinanceAdapter]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_adapters_pass_inherited_exchange_identity[BybitAdapter]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_adapters_pass_inherited_exchange_identity[BitgetAdapter]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_public_clients_fail_closed_without_proxy[bapi]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_public_clients_fail_closed_without_proxy[fapi]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_binance_public_clients_fail_closed_without_proxy[analysis]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[sync_account-okx-True]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[sync_account-binance-True]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[sync_account-okx-False]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[risk_tick-okx-True]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[risk_tick-binance-True]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[risk_tick-okx-False]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[router-okx-True]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[router-binance-True]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[router-okx-False]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[connectivity-okx-True]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[connectivity-binance-True]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[connectivity-okx-False]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[flat-okx-True]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[flat-binance-True]` | PASSED |
| `tests/test_exchange_egress_policy.py::test_account_proxy_applies_policy_at_every_entry_point[flat-okx-False]` | PASSED |
| `tests/test_execution_persistence.py::test_existing_file_database_gets_session_and_ledger_upgrade` | PASSED |
| `tests/test_execution_persistence.py::test_executor_serializes_tasks_and_is_reentrant` | PASSED |
| `tests/test_execution_persistence.py::test_persisted_invalid_risk_configuration_is_rejected` | PASSED |
| `tests/test_health_accounting.py::test_liveness_does_not_probe_database_or_engine` | PASSED |
| `tests/test_health_accounting.py::test_ready_requires_reachable_database_and_fresh_engine` | PASSED |
| `tests/test_health_accounting.py::test_absent_stale_stopped_or_disabled_engine_is_degraded[None-True-True]` | PASSED |
| `tests/test_health_accounting.py::test_absent_stale_stopped_or_disabled_engine_is_degraded[31.0-True-True]` | PASSED |
| `tests/test_health_accounting.py::test_absent_stale_stopped_or_disabled_engine_is_degraded[1.0-False-True]` | PASSED |
| `tests/test_health_accounting.py::test_absent_stale_stopped_or_disabled_engine_is_degraded[1.0-True-False]` | PASSED |
| `tests/test_health_accounting.py::test_database_failure_is_degraded_without_connection_details` | PASSED |
| `tests/test_health_accounting.py::test_database_probe_timeout_is_bounded` | PASSED |
| `tests/test_health_accounting.py::test_every_loop_tick_updates_monotonic_heartbeat` | PASSED |
| `tests/test_health_accounting.py::test_execution_fee_funding_and_fill_identifier_persist` | PASSED |
| `tests/test_health_accounting.py::test_existing_execution_rows_survive_idempotent_accounting_upgrade` | PASSED |
| `tests/test_health_accounting.py::test_cash_flow_adjusted_pnl_removes_transfers_and_unrealized_growth[1000-1280-kwargs0-50]` | PASSED |
| `tests/test_health_accounting.py::test_cash_flow_adjusted_pnl_removes_transfers_and_unrealized_growth[1000-920-kwargs1-20]` | PASSED |
| `tests/test_health_accounting.py::test_cash_flow_adjusted_pnl_removes_transfers_and_unrealized_growth[1000-1037.9-kwargs2-27.9]` | PASSED |
| `tests/test_health_accounting.py::test_cash_flow_adjusted_pnl_removes_transfers_and_unrealized_growth[1000-1027-kwargs3-27]` | PASSED |
| `tests/test_health_accounting.py::test_cash_flow_adjusted_pnl_rejects_invalid_accounting_inputs[kwargs0]` | PASSED |
| `tests/test_health_accounting.py::test_cash_flow_adjusted_pnl_rejects_invalid_accounting_inputs[kwargs1]` | PASSED |
| `tests/test_health_accounting.py::test_cash_flow_adjusted_pnl_rejects_invalid_accounting_inputs[kwargs2]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/settings-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/settings-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/settings-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/settings/apply-defaults-to-bindings-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/settings/apply-defaults-to-bindings-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/settings/apply-defaults-to-bindings-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/settings/test-notify-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/settings/test-notify-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/settings/test-notify-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/settings/security-guards/{guard}/unlock-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/settings/security-guards/{guard}/unlock-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/settings/security-guards/{guard}/unlock-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/proxies-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/proxies-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/proxies-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/proxies/{proxy}-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/proxies/{proxy}-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/proxies/{proxy}-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[DELETE /api/proxies/{proxy}-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[DELETE /api/proxies/{proxy}-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[DELETE /api/proxies/{proxy}-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/proxies/{proxy}/test-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/proxies/{proxy}/test-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/proxies/{proxy}/test-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/accounts-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/accounts-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/accounts-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/accounts/{account}-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/accounts/{account}-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/accounts/{account}-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[DELETE /api/accounts/{account}-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[DELETE /api/accounts/{account}-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[DELETE /api/accounts/{account}-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/accounts/{account}/test-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/accounts/{account}/test-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/accounts/{account}/test-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/accounts/{account}/sync-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/accounts/{account}/sync-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/accounts/{account}/sync-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/leads-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/leads-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/leads-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/leads/{lead}-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/leads/{lead}-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/leads/{lead}-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[DELETE /api/leads/{lead}-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[DELETE /api/leads/{lead}-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[DELETE /api/leads/{lead}-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/leads/{lead}/sync-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/leads/{lead}/sync-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/leads/{lead}/sync-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/bindings/{binding}-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/bindings/{binding}-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[PUT /api/bindings/{binding}-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[DELETE /api/bindings/{binding}-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[DELETE /api/bindings/{binding}-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[DELETE /api/bindings/{binding}-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings/{binding}/toggle-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings/{binding}/toggle-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings/{binding}/toggle-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings/{binding}/reset-breaker-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings/{binding}/reset-breaker-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings/{binding}/reset-breaker-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings/{binding}/reset-debt-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings/{binding}/reset-debt-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings/{binding}/reset-debt-admin]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings/kill-switch-viewer]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings/kill-switch-anonymous]` | PASSED |
| `tests/test_management_roles.py::test_management_mutations_require_current_admin[POST /api/bindings/kill-switch-admin]` | PASSED |
| `tests/test_management_roles.py::test_viewers_retain_read_only_management_access[/api/settings]` | PASSED |
| `tests/test_management_roles.py::test_viewers_retain_read_only_management_access[/api/settings/security-guards]` | PASSED |
| `tests/test_management_roles.py::test_viewers_retain_read_only_management_access[/api/settings/audit-logs]` | PASSED |
| `tests/test_management_roles.py::test_viewers_retain_read_only_management_access[/api/proxies]` | PASSED |
| `tests/test_management_roles.py::test_viewers_retain_read_only_management_access[/api/proxies/{proxy}]` | PASSED |
| `tests/test_management_roles.py::test_viewers_retain_read_only_management_access[/api/accounts]` | PASSED |
| `tests/test_management_roles.py::test_viewers_retain_read_only_management_access[/api/leads]` | PASSED |
| `tests/test_management_roles.py::test_viewers_retain_read_only_management_access[/api/leads/{lead}/snapshot]` | PASSED |
| `tests/test_management_roles.py::test_viewers_retain_read_only_management_access[/api/leads/{lead}/analysis]` | PASSED |
| `tests/test_management_roles.py::test_viewers_retain_read_only_management_access[/api/leads/{lead}/export-csv]` | PASSED |
| `tests/test_management_roles.py::test_viewers_retain_read_only_management_access[/api/bindings]` | PASSED |
| `tests/test_management_roles.py::test_viewers_retain_read_only_management_access[/api/dashboard/overview]` | PASSED |
| `tests/test_management_roles.py::test_admin_gate_uses_current_database_role_over_token_claim` | PASSED |
| `tests/test_management_roles.py::test_viewer_can_receive_broadcasts_and_ping` | PASSED |
| `tests/test_notification_outbox.py::test_rollback_removes_execution_and_outbox` | PASSED |
| `tests/test_notification_outbox.py::test_commit_enqueues_once_before_broadcast_without_dispatch` | PASSED |
| `tests/test_notification_outbox.py::test_drainer_marks_sent_and_does_not_send_again` | PASSED |
| `tests/test_notification_outbox.py::test_failed_sink_retries_with_backoff_then_enters_dead_letter_queue` | PASSED |
| `tests/test_notification_outbox.py::test_successful_channels_are_not_duplicated_when_another_sink_retries` | PASSED |
| `tests/test_notification_outbox.py::test_restart_recovers_expired_claims_and_exhausted_attempts` | PASSED |
| `tests/test_notification_outbox.py::test_workers_cannot_claim_the_same_pending_row` | PASSED |
| `tests/test_notification_outbox.py::test_graceful_shutdown_drains_pending_rows_and_finishes_retries` | PASSED |
| `tests/test_notification_outbox.py::test_security_alert_joins_login_transaction_and_rolls_back` | PASSED |
| `tests/test_notification_outbox.py::test_sink_exception_details_never_escape_dispatch_or_dead_letter_queue` | PASSED |
| `tests/test_notification_outbox.py::test_enqueue_redacts_embedded_proxy_userinfo` | PASSED |
| `tests/test_notification_outbox.py::test_shutdown_database_failure_retains_work_without_masking_shutdown` | PASSED |
| `tests/test_notification_outbox.py::test_jsonl_sink_runs_file_io_off_event_loop` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_direct_egress_defaults_to_enabled` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_healthy_preferred_proxy_is_selected_first` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_unavailable_preferred_proxy_falls_through[False-True]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_unavailable_preferred_proxy_falls_through[True-False]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_failover_orders_only_healthy_nodes_without_direct_egress` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_unavailable_egress_fails_closed[select_proxy_url-nodes0]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_unavailable_egress_fails_closed[select_proxy_url-nodes1]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_unavailable_egress_fails_closed[select_proxy_url-nodes2]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_unavailable_egress_fails_closed[ordered_proxy_urls-nodes0]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_unavailable_egress_fails_closed[ordered_proxy_urls-nodes1]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_unavailable_egress_fails_closed[ordered_proxy_urls-nodes2]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_explicit_opt_in_permits_direct_when_no_healthy_proxy[nodes0]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_explicit_opt_in_permits_direct_when_no_healthy_proxy[nodes1]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_explicit_opt_in_permits_direct_when_no_healthy_proxy[nodes2]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_direct_fallback_is_only_appended_after_explicit_opt_in` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_client_rejects_direct_bypass_before_creating_transport` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_client_uses_explicit_proxy_or_authorized_direct[False]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_client_uses_explicit_proxy_or_authorized_direct[True]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_unassigned_account_uses_configured_proxy_for_connectivity` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_account_connectivity_reports_denied_egress_without_adapter` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_flat_account_verification_uses_configured_proxy` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_lead_analysis_uses_proxy_and_refreshes_saved_analysis[False]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_lead_analysis_uses_proxy_and_refreshes_saved_analysis[True]` | PASSED |
| `tests/test_proxy_egress_remediations.py::test_lead_analysis_reports_denied_egress_without_network` | PASSED |
| `tests/test_router_remediations.py::test_ambiguous_order_is_never_resent[None]` | PASSED |
| `tests/test_router_remediations.py::test_ambiguous_order_is_never_resent[lookup_result1]` | PASSED |
| `tests/test_router_remediations.py::test_reconciled_placement_uses_existing_order` | PASSED |
| `tests/test_router_remediations.py::test_full_close_only_consumes_this_bindings_allocation` | PASSED |
| `tests/test_router_remediations.py::test_close_cannot_claim_manual_or_unallocated_positions` | PASSED |
| `tests/test_router_remediations.py::test_small_partial_close_does_not_promote_to_minimum` | PASSED |
| `tests/test_router_remediations.py::test_partial_close_scales_only_owned_quantity` | PASSED |
| `tests/test_router_remediations.py::test_zero_close_fill_never_clears_debt_or_guard[canceled]` | PASSED |
| `tests/test_router_remediations.py::test_zero_close_fill_never_clears_debt_or_guard[new]` | PASSED |
| `tests/test_router_remediations.py::test_historical_open_is_skipped_when_lead_is_flat` | PASSED |
| `tests/test_router_remediations.py::test_catchup_uses_executable_side_and_symmetric_drift[BUY-LONG-100.4-100.6-BLOCKED_CATCHUP_DRIFT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_uses_executable_side_and_symmetric_drift[SELL-SHORT-99.4-99.6-BLOCKED_CATCHUP_DRIFT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_uses_executable_side_and_symmetric_drift[BUY-LONG-99.4-99.6-EXECUTED]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_uses_executable_side_and_symmetric_drift[SELL-SHORT-100.4-100.6-EXECUTED]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_uses_executable_side_and_symmetric_drift[BUY-LONG-99.3-99.4-BLOCKED_CATCHUP_DRIFT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_uses_executable_side_and_symmetric_drift[SELL-SHORT-100.6-100.7-BLOCKED_CATCHUP_DRIFT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_missing_or_invalid_executable_quote_is_recorded[None-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_missing_or_invalid_executable_quote_is_recorded[None-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_missing_or_invalid_executable_quote_is_recorded[0-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_missing_or_invalid_executable_quote_is_recorded[0-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_missing_or_invalid_executable_quote_is_recorded[-1-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_missing_or_invalid_executable_quote_is_recorded[-1-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_missing_or_invalid_executable_quote_is_recorded[nan-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_missing_or_invalid_executable_quote_is_recorded[nan-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_missing_or_invalid_executable_quote_is_recorded[inf-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_missing_or_invalid_executable_quote_is_recorded[inf-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_empty_book_cannot_reuse_lead_price[BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_empty_book_cannot_reuse_lead_price[SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_valid_executable_quote_still_requires_valid_opposite_book[None-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_valid_executable_quote_still_requires_valid_opposite_book[None-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_valid_executable_quote_still_requires_valid_opposite_book[0-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_valid_executable_quote_still_requires_valid_opposite_book[0-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_valid_executable_quote_still_requires_valid_opposite_book[-1-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_valid_executable_quote_still_requires_valid_opposite_book[-1-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_valid_executable_quote_still_requires_valid_opposite_book[nan-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_valid_executable_quote_still_requires_valid_opposite_book[nan-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_valid_executable_quote_still_requires_valid_opposite_book[inf-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_valid_executable_quote_still_requires_valid_opposite_book[inf-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_invalid_lead_price_is_recorded[None-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_invalid_lead_price_is_recorded[None-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_invalid_lead_price_is_recorded[0-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_invalid_lead_price_is_recorded[0-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_invalid_lead_price_is_recorded[-1-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_invalid_lead_price_is_recorded[-1-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_invalid_lead_price_is_recorded[nan-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_invalid_lead_price_is_recorded[nan-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_invalid_lead_price_is_recorded[inf-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_invalid_lead_price_is_recorded[inf-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_invalid_lead_price_is_recorded[-inf-BUY-LONG]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_invalid_lead_price_is_recorded[-inf-SELL-SHORT]` | PASSED |
| `tests/test_router_remediations.py::test_invalid_catchup_limits_are_recorded_before_execution[0-missed_order_max_age_sec]` | PASSED |
| `tests/test_router_remediations.py::test_invalid_catchup_limits_are_recorded_before_execution[0-catchup_max_price_dev_pct]` | PASSED |
| `tests/test_router_remediations.py::test_invalid_catchup_limits_are_recorded_before_execution[-1-missed_order_max_age_sec]` | PASSED |
| `tests/test_router_remediations.py::test_invalid_catchup_limits_are_recorded_before_execution[-1-catchup_max_price_dev_pct]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_missing_or_future_timestamp_cannot_bypass_gate[0]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_missing_or_future_timestamp_cannot_bypass_gate[-1]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_missing_or_future_timestamp_cannot_bypass_gate[2000001]` | PASSED |
| `tests/test_router_remediations.py::test_catchup_rejects_before_spread_slippage_sizing_and_leverage` | PASSED |
| `tests/test_router_remediations.py::test_old_exit_event_cannot_close_newer_allocation` | PASSED |
| `tests/test_router_remediations.py::test_closed_cycle_open_cannot_attach_to_current_reopened_position` | PASSED |
| `tests/test_router_remediations.py::test_fresh_snapshot_routes_150_second_delayed_fill_once` | PASSED |
| `tests/test_router_remediations.py::test_flip_closes_long_before_catchup_short_for_both_legacy_flags[False]` | PASSED |
| `tests/test_router_remediations.py::test_flip_closes_long_before_catchup_short_for_both_legacy_flags[True]` | PASSED |
| `tests/test_router_remediations.py::test_leverage_is_dynamic_and_quantity_uses_equity_ratio` | PASSED |
| `tests/test_router_remediations.py::test_failed_leverage_confirmation_prevents_order` | PASSED |
| `tests/test_router_remediations.py::test_restart_reconciles_durable_intent_without_second_post` | PASSED |
| `tests/test_router_remediations.py::test_old_closed_cycle_cannot_close_reopened_allocation` | PASSED |
| `tests/test_router_remediations.py::test_closed_history_reconciliation_is_binding_scoped` | PASSED |
| `tests/test_router_remediations.py::test_orphan_close_is_binding_scoped` | PASSED |
| `tests/test_router_remediations.py::test_protective_stop_only_closes_binding_allocation` | PASSED |
| `tests/test_router_remediations.py::test_missing_protective_quote_blocks_without_faking_entry_price` | PASSED |
| `tests/test_router_remediations.py::test_partial_ioc_then_terminal_does_not_send_market_fallback` | PASSED |
| `tests/test_router_remediations.py::test_missing_cumulative_field_can_use_confirmed_submission_fill` | PASSED |
| `tests/test_router_remediations.py::test_crash_after_exchange_fill_before_commit_recovers_without_resend` | PASSED |
| `tests/test_routing_deliveries.py::test_new_binding_baseline_does_not_consume_established_followers_event` | PASSED |
| `tests/test_routing_deliveries.py::test_empty_baseline_still_delivers_first_real_order` | PASSED |
| `tests/test_routing_deliveries.py::test_failed_follower_delivery_survives_global_source_ack_and_retry` | PASSED |
| `tests/test_routing_deliveries.py::test_cumulative_source_updates_deliver_only_incremental_fill` | PASSED |
| `tests/test_routing_deliveries.py::test_deliveries_preserve_chronological_open_close_open_order` | PASSED |
| `tests/test_routing_deliveries.py::test_legacy_float_hash_cursor_is_migrated_without_replay` | PASSED |
| `tests/test_routing_deliveries.py::test_discarded_order_id_migration_retains_cumulative_progress` | PASSED |
| `tests/test_routing_deliveries.py::test_independent_monitor_latches_loss_breaker_and_keeps_disabled_protection` | PASSED |
| `tests/test_routing_deliveries.py::test_snapshot_publication_and_failure_never_overwrite_newer_success` | PASSED |
| `tests/test_routing_deliveries.py::test_newer_source_failure_invalidates_cached_authoritative_snapshot` | PASSED |
| `tests/test_routing_deliveries.py::test_newer_source_failure_invalidates_older_inflight_router_snapshot` | PASSED |
| `tests/test_secret_mask.py::test_mask_secret_never_discloses_even_short_values[None]` | PASSED |
| `tests/test_secret_mask.py::test_mask_secret_never_discloses_even_short_values[]` | PASSED |
| `tests/test_secret_mask.py::test_mask_secret_never_discloses_even_short_values[a]` | PASSED |
| `tests/test_secret_mask.py::test_mask_secret_never_discloses_even_short_values[1234]` | PASSED |
| `tests/test_secret_mask.py::test_mask_secret_never_discloses_even_short_values[synthetic-long-secret]` | PASSED |
| `tests/test_secret_mask.py::test_mask_sentinel_is_distinct_from_new_values` | PASSED |
| `tests/test_secret_mask.py::test_merge_secrets_preserves_omitted_empty_and_masked_values` | PASSED |
| `tests/test_secret_mask.py::test_proxy_masks_and_updates_round_trip[参数标识:7136171bfc97]` | PASSED |
| `tests/test_secret_mask.py::test_proxy_masks_and_updates_round_trip[socks5://user%40name:pass%3Aword@[::1]:1080-socks5://***:***@[::1]:1080]` | PASSED |
| `tests/test_secret_mask.py::test_proxy_masks_and_updates_round_trip[参数标识:92fdb07fb196]` | PASSED |
| `tests/test_secret_mask.py::test_proxy_masks_and_updates_round_trip[参数标识:1149482b6998]` | PASSED |
| `tests/test_secret_mask.py::test_proxy_mask_cannot_forward_stored_credentials_to_another_host` | PASSED |
| `tests/test_secret_mask.py::test_notification_secrets_masked_on_get_and_put_and_round_trip` | PASSED |
| `tests/test_secret_mask.py::test_unconfigured_notifications_have_false_flags` | PASSED |
| `tests/test_secret_mask.py::test_proxy_secrets_hidden_on_all_api_reads_and_preserved_on_update` | PASSED |
| `tests/test_secret_mask.py::test_nested_telemetry_masks_explicit_secret_keys_and_proxy_errors` | PASSED |
| `tests/test_secret_mask.py::test_websocket_broadcast_masks_proxy_credentials` | PASSED |
| `tests/test_security.py::test_password_hashing` | PASSED |
| `tests/test_security.py::test_aes_gcm_encryption_roundtrip` | PASSED |
| `tests/test_security.py::test_credential_vault` | PASSED |
| `tests/test_security.py::test_jwt_tokens` | PASSED |
| `tests/test_security.py::test_captcha_generation_and_validation` | PASSED |
| `tests/test_security.py::test_totp_generation_and_verification` | PASSED |
| `tests/test_security_remediations.py::test_access_tokens_reject_captcha_wrong_purpose_and_missing_claims` | PASSED |
| `tests/test_security_remediations.py::test_spa_contains_encoded_traversals_and_symlinks` | PASSED |
| `tests/test_security_remediations.py::test_bootstrap_requires_operator_token_and_is_atomic` | PASSED |
| `tests/test_security_remediations.py::test_password_and_logout_revoke_copied_bearer_tokens` | PASSED |
| `tests/test_security_remediations.py::test_mfa_requires_password_existing_factor_and_revokes_sessions` | PASSED |
| `tests/test_security_remediations.py::test_mfa_corrupted_or_expired_state_fails_closed` | PASSED |
| `tests/test_security_remediations.py::test_websocket_validates_origin_user_version_and_rechecks_before_broadcast` | PASSED |
| `tests/test_security_remediations.py::test_global_risk_and_resilience_reject_unsafe_values_on_write_and_apply` | PASSED |
| `tests/test_security_remediations.py::test_secret_file_creation_is_atomic_private_and_does_not_replace_empty_keys` | PASSED |
| `tests/test_security_remediations.py::test_missing_database_key_and_weak_configured_key_fail_closed` | PASSED |
| `tests/test_security_remediations.py::test_configured_console_origins_accept_documented_environment_format` | PASSED |
| `tests/test_security_remediations.py::test_startup_refuses_credentials_encrypted_by_another_key` | PASSED |
| `tests/test_security_remediations.py::test_lifecycle_changes_preserve_owned_and_unknown_exposure` | PASSED |
| `tests/test_settings_catchup.py::test_catchup_settings_round_trip[900-0.5]` | PASSED |
| `tests/test_settings_catchup.py::test_catchup_settings_round_trip[120-0.8]` | PASSED |
| `tests/test_settings_catchup.py::test_catchup_settings_round_trip[10-0.01]` | PASSED |
| `tests/test_settings_catchup.py::test_catchup_settings_round_trip[11-0.05]` | PASSED |
| `tests/test_settings_catchup.py::test_catchup_settings_round_trip[3600-10]` | PASSED |
| `tests/test_settings_catchup.py::test_catchup_settings_reject_values_outside_schema[missed_order_max_age_sec-9]` | PASSED |
| `tests/test_settings_catchup.py::test_catchup_settings_reject_values_outside_schema[missed_order_max_age_sec-3601]` | PASSED |
| `tests/test_settings_catchup.py::test_catchup_settings_reject_values_outside_schema[missed_order_max_age_sec-10.5]` | PASSED |
| `tests/test_settings_catchup.py::test_catchup_settings_reject_values_outside_schema[catchup_max_price_dev_pct-0]` | PASSED |
| `tests/test_settings_catchup.py::test_catchup_settings_reject_values_outside_schema[catchup_max_price_dev_pct--0.1]` | PASSED |
| `tests/test_settings_catchup.py::test_catchup_settings_reject_values_outside_schema[catchup_max_price_dev_pct-10.1]` | PASSED |
| `tests/test_settings_catchup.py::test_catchup_settings_reject_values_outside_schema[catchup_max_price_dev_pct-NaN]` | PASSED |
| `tests/test_settings_catchup.py::test_catchup_settings_reject_values_outside_schema[catchup_max_price_dev_pct-Infinity]` | PASSED |
| `tests/test_settings_catchup.py::test_catchup_schema_and_database_defaults_agree` | PASSED |
| `tests/test_settings_catchup.py::test_partial_put_preserves_operator_pinned_catchup_values` | PASSED |
| `tests/test_settings_catchup.py::test_legacy_catchup_settings_are_not_rewritten_and_keep_their_policy` | PASSED |
| `tests/test_sizer_risk.py::test_quantize_and_format` | PASSED |
| `tests/test_sizer_risk.py::test_position_sizer_proportional` | PASSED |
| `tests/test_sizer_risk.py::test_risk_engine_checks` | PASSED |
| `tests/test_sizer_risk.py::test_missed_order_catchup_and_stale_guards` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_allows_exact_drift_and_age_boundaries[99.5]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_allows_exact_drift_and_age_boundaries[100.5]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_absolute_drift_precedes_age_backstop[0-99.49]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_absolute_drift_precedes_age_backstop[0-100.51]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_absolute_drift_precedes_age_backstop[150000-99.49]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_absolute_drift_precedes_age_backstop[150000-100.51]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_absolute_drift_precedes_age_backstop[1000000-99.49]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_absolute_drift_precedes_age_backstop[1000000-100.51]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_requires_valid_prices[0-ref_price]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_requires_valid_prices[0-lead_avg_price]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_requires_valid_prices[-1-ref_price]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_requires_valid_prices[-1-lead_avg_price]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_requires_valid_prices[nan-ref_price]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_requires_valid_prices[nan-lead_avg_price]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_requires_valid_prices[inf-ref_price]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_requires_valid_prices[inf-lead_avg_price]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_requires_valid_prices[-inf-ref_price]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_requires_valid_prices[-inf-lead_avg_price]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_limits_cannot_disable_guards[0-max_age_sec]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_limits_cannot_disable_guards[0-catchup_max_price_dev_pct]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_limits_cannot_disable_guards[-1-max_age_sec]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_limits_cannot_disable_guards[-1-catchup_max_price_dev_pct]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_limits_cannot_disable_guards[nan-max_age_sec]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_limits_cannot_disable_guards[nan-catchup_max_price_dev_pct]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_limits_cannot_disable_guards[inf-max_age_sec]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_limits_cannot_disable_guards[inf-catchup_max_price_dev_pct]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_limits_cannot_disable_guards[-inf-max_age_sec]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_limits_cannot_disable_guards[-inf-catchup_max_price_dev_pct]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_timestamp_is_stale[0]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_timestamp_is_stale[-1]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_timestamp_is_stale[2000001]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_timestamp_is_stale[nan]` | PASSED |
| `tests/test_sizer_risk.py::test_catchup_invalid_timestamp_is_stale[inf]` | PASSED |
| `tests/test_sizer_risk.py::test_insufficient_margin_auto_scale_down` | PASSED |
| `tests/test_sizer_risk.py::test_tp_sl_price_calculation` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_exact_proportional_target_is_independent_of_leverage_and_floors_once` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_nonfinite_financial_inputs_fail_closed[NaN]` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_nonfinite_financial_inputs_fail_closed[Infinity]` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_nonfinite_financial_inputs_fail_closed[-Infinity]` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_partial_minimum_position_is_never_promoted_to_full_close` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_dynamic_leverage_tracks_exposure_and_symbol_ceiling` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_risk_guards_reject_missing_crossed_books_and_invalid_event_time` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_caps_revalidate_minimum_notional` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_mutable_fill_fields_preserve_identity_and_ids_distinguish_orders` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_batches_preserve_open_close_open_causality_and_decimal_totals` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_reconstructed_partial_close_reduces_cost_and_old_cycle_cannot_hide_reopen` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_direct_holdings_unknown_rows_and_stale_snapshots_fail_closed` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_source_store_updates_cumulative_fill_without_duplicate_or_precision_loss` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_missing_history_page_never_partially_advances_store` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_snapshot_uses_fresh_direct_holdings_and_no_execution_analytics` | PASSED |
| `tests/test_sizing_collector_remediations.py::test_legacy_store_adopts_restored_order_id_without_duplicating_order` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_nested_stored_telemetry_masks_proxy_credentials[snapshot]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_nested_stored_telemetry_masks_proxy_credentials[public]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_nested_stored_telemetry_masks_proxy_credentials[overview]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_nested_stored_telemetry_masks_proxy_credentials[accounts]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_raw_sync_results_are_masked_recursively[lead]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_raw_sync_results_are_masked_recursively[account]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_account_test_redacts_credentials_before_error_truncation` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_proxy_test_redacts_credentials_before_error_truncation` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_collector_masks_long_userinfo_before_storing_holdings_error` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_collector_masks_http_error_body_before_truncation` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_error_redaction_handles_historical_truncation_and_whitespace[参数标识:7d40c478ab2b]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_error_redaction_handles_historical_truncation_and_whitespace[参数标识:726fff08e3df]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_error_redaction_handles_historical_truncation_and_whitespace[参数标识:b452fb01171b]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_error_redaction_handles_historical_truncation_and_whitespace[Failure socks5://synthetic-user:synthetic-pass\nsecond-fragment]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_recursive_error_fields_hide_partial_credentials[circuitBreakerReason]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_recursive_error_fields_hide_partial_credentials[circuit_breaker_reason]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_recursive_error_fields_hide_partial_credentials[detail]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_recursive_error_fields_hide_partial_credentials[details]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_recursive_error_fields_hide_partial_credentials[error]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_recursive_error_fields_hide_partial_credentials[errors]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_recursive_error_fields_hide_partial_credentials[lastError]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_recursive_error_fields_hide_partial_credentials[last_error]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_recursive_error_fields_hide_partial_credentials[reason]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_actual_proxy_urls_mask_literal_whitespace_userinfo_and_keep_host` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_malformed_proxy_urls_fail_safe[参数标识:34fdab40deea]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_malformed_proxy_urls_fail_safe[参数标识:6264a9db5f1f]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_historical_partial_credentials_hidden_on_all_diagnostic_reads[proxies]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_historical_partial_credentials_hidden_on_all_diagnostic_reads[proxy_detail]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_historical_partial_credentials_hidden_on_all_diagnostic_reads[snapshot]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_historical_partial_credentials_hidden_on_all_diagnostic_reads[public]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_historical_partial_credentials_hidden_on_all_diagnostic_reads[overview]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_historical_partial_credentials_hidden_on_all_diagnostic_reads[executions]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_historical_partial_credentials_hidden_on_all_diagnostic_reads[accounts]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_historical_partial_credentials_hidden_on_all_diagnostic_reads[leads]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_historical_partial_credentials_hidden_on_all_diagnostic_reads[bindings]` | PASSED |
| `tests/test_telemetry_secret_remediations.py::test_historical_partial_credentials_hidden_on_all_diagnostic_reads[audit]` | PASSED |
