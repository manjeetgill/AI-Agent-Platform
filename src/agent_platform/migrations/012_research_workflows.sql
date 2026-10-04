ALTER TABLE agent_workflow_runs DROP CONSTRAINT agent_workflow_runs_kind_check;
ALTER TABLE agent_workflow_runs ADD CONSTRAINT agent_workflow_runs_kind_check CHECK(kind IN ('signal_review','backtest_review','trading_review','strategy_comparison'));
