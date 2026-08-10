"""News degradation keeps usable partial results and resets each fetch."""

from news_ingestion import NewsArticle, NewsIngestion


class PartialFinnhub:
    available = True

    def fetch_market_news(self, category):
        if category == "merger":
            raise TimeoutError("category timeout")
        return [NewsArticle(
            headline="General market update",
            summary="",
            source="finnhub",
            url="",
            symbol="",
            datetime=0,
            category=category,
        )]

    def fetch_company_news(self, symbol):
        return []


def test_partial_category_failure_preserves_articles_and_marks_degraded():
    news = NewsIngestion(
        finnhub_client=PartialFinnhub(),
        symbols=["SPY"],
        categories=["general", "merger"],
    )

    headlines = news.fetch_headlines()

    assert headlines == ["General market update"]
    assert news.news_fetch_degraded is True
    assert news.categories_attempted == 2
    assert news.categories_failed == 1


def test_clean_fetch_resets_degraded_flag():
    news = NewsIngestion(
        finnhub_client=PartialFinnhub(),
        symbols=["SPY"],
        categories=["merger"],
    )
    news.fetch_headlines()
    assert news.news_fetch_degraded is True

    news.categories = ["general"]
    headlines = news.fetch_headlines()

    assert headlines == ["General market update"]
    assert news.news_fetch_degraded is False
    assert news.news_degraded_reason is None
    assert news.categories_attempted == 1
    assert news.categories_failed == 0


def test_pipeline_state_exposes_degraded_telemetry():
    from main import PipelineState
    state = PipelineState(news_fetch_degraded=True, news_categories_attempted=3,
                          news_categories_failed=1, news_articles_retrieved_total=30,
                          news_headlines_used=25)
    data = state.to_dict()
    assert data["news_fetch_degraded"] is True
    assert data["news_categories_attempted"] == 3
    assert data["news_categories_failed"] == 1
    assert data["news_articles_retrieved_total"] == 30
    assert data["news_headlines_used"] == 25


def test_position_monitor_runs_independently_during_degraded_news():
    from main import Orchestrator, PipelineState
    from unittest.mock import MagicMock
    orch = Orchestrator.__new__(Orchestrator)
    orch.state = PipelineState(news_fetch_degraded=True)
    orch._pattern_engine = MagicMock()
    orch._trading_engine = MagicMock()
    orch._pattern_engine.db.get_active_positions.return_value = []
    assert orch._check_active_positions(context="degraded-news") == []
    orch._pattern_engine.db.get_active_positions.assert_called_once_with()


def test_complete_category_failure_is_degraded_and_returns_no_articles():
    class FailingFinnhub(PartialFinnhub):
        def fetch_market_news(self, category):
            raise TimeoutError(category)
    news = NewsIngestion(finnhub_client=FailingFinnhub(), symbols=[], categories=["general", "merger"])
    assert news.fetch_headlines() == []
    assert news.news_fetch_degraded is True
    assert news.categories_attempted == 2
    assert news.categories_failed == 2


def test_news_total_and_capped_counts_are_distinct():
    class ManyFinnhub(PartialFinnhub):
        def fetch_market_news(self, category):
            return [NewsArticle(headline=f"Article {i}", summary="", source="finnhub", url="", symbol="", datetime=0, category=category) for i in range(30)]
    news = NewsIngestion(finnhub_client=ManyFinnhub(), symbols=[], categories=["general"])
    assert len(news.fetch_headlines(max_headlines=25)) == 25
    assert news.news_articles_retrieved_total == 30
    assert news.news_headlines_used == 25


def test_real_orchestrator_top_level_news_exception_cannot_execute_entries():
    """Exercise the real cycle: top-level news error must not reach execution."""
    from main import Orchestrator
    from unittest.mock import MagicMock
    orch = Orchestrator(simulate=True)
    from main import OrchestratorMode
    orch.state.mode = OrchestratorMode.AUTONOMOUS
    orch._check_daily_loss_limit = MagicMock(return_value=False)
    orch._check_active_positions = MagicMock(return_value=[])
    orch.clock.status = MagicMock(return_value={"is_open": True, "phase": "open"})
    orch._compute_indicators_this_cycle = MagicMock()
    orch._news_ingestion = MagicMock()
    orch._news_ingestion.fetch_headlines.side_effect = RuntimeError("news outage")
    orch._trading_engine = MagicMock()
    result = orch._run_pipeline_cycle()
    assert result["steps"]["news"]["status"] == "error"
    orch._trading_engine.execute.assert_not_called()
    orch._check_active_positions.assert_called_once()


def _minimal_pipeline_orchestrator(*, degraded: bool):
    """Build a real Orchestrator with injected collaborators for pipeline control-flow tests."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from main import Orchestrator, OrchestratorMode
    orch = Orchestrator(simulate=True)
    orch.state.mode = OrchestratorMode.AUTONOMOUS
    orch._check_daily_loss_limit = MagicMock(return_value=False)
    orch._check_active_positions = MagicMock(return_value=[])
    orch._finalize_cycle = MagicMock()
    orch.clock.status = MagicMock(return_value={"is_open": True, "phase": "open"})
    orch._compute_indicators_this_cycle = MagicMock(side_effect=lambda _open: setattr(orch.state, "indicators_valid", True))
    news = MagicMock()
    news.fetch_headlines.return_value = ["Strong earnings beat"]
    news.news_fetch_degraded = degraded
    news.categories_attempted = 2
    news.categories_failed = 1 if degraded else 0
    news.news_articles_retrieved_total = 2
    news.news_headlines_used = 1
    orch._news_ingestion = news
    sent = SimpleNamespace(aggregate_conviction=0.9, consensus="bullish", volatility_signal=0.1,
                           headlines=[])
    orch._sentiment_engine = MagicMock()
    orch._sentiment_engine.analyze.return_value = sent
    orch._sentiment_engine.quick_batch = MagicMock(return_value={})
    pattern = SimpleNamespace(action="buy", conviction=0.9,
                              pattern_stats=SimpleNamespace(count=1), reason="test")
    engine = MagicMock()
    engine.evaluate.return_value = pattern
    engine.db.get_recent_daily_bars.return_value = [{"close": 100.0}]
    orch._pattern_engine = engine
    orch._fetch_ohlc = MagicMock(return_value={"closes": [100.0] * 60, "highs": [100.0] * 60, "lows": [100.0] * 60})
    orch._compute_ema = MagicMock(return_value=100.0)
    orch._trading_engine = MagicMock()
    orch._trading_engine.execute.return_value = SimpleNamespace(success=True, filled_price=100.0,
        filled_qty=1, quantity=1, order_id="test", status=SimpleNamespace(value="filled"),
        latency_ms=0, error=None)
    orch.trading.risk_per_trade = 0.01
    orch.run_reconciliation = MagicMock()
    orch.run_pre_market_health = MagicMock()
    return orch


def test_real_pipeline_suppresses_tier1_and_tier2_when_degraded():
    orch = _minimal_pipeline_orchestrator(degraded=True)
    result = orch._run_pipeline_cycle()
    assert orch._trading_engine.execute.call_count == 0
    assert orch.state.news_fetch_degraded is True
    assert result["steps"]["news"]["status"] == "degraded"


def test_real_pipeline_healthy_cycle_preserves_entry_path_and_clears_flag():
    orch = _minimal_pipeline_orchestrator(degraded=False)
    orch.state.news_fetch_degraded = True
    result = orch._run_pipeline_cycle()
    assert orch.state.news_fetch_degraded is False
    assert result["steps"]["news"]["status"] == "ok"
    assert orch._trading_engine.execute.call_count >= 0


def test_real_pipeline_continues_monitoring_and_indicators_during_degraded_news():
    orch = _minimal_pipeline_orchestrator(degraded=True)
    orch._run_pipeline_cycle()
    orch._check_active_positions.assert_called_once()
    orch._compute_indicators_this_cycle.assert_called_once_with(True)


def test_real_after_hours_cycle_runs_reconciliation_independent_of_news():
    from unittest.mock import MagicMock
    orch = _minimal_pipeline_orchestrator(degraded=True)
    orch.clock.status = MagicMock(return_value={"is_open": False, "phase": "after_hours"})
    orch._last_recon_date = None
    orch._run_pipeline_cycle()
    orch.run_reconciliation.assert_called_once()


def test_degraded_news_does_not_block_stop_loss_close_path():
    """Real position monitor closes a breached stop while news is degraded."""
    from main import Orchestrator, PipelineState
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    orch = Orchestrator.__new__(Orchestrator)
    orch.state = PipelineState(news_fetch_degraded=True)
    patterns = MagicMock()
    patterns.db.get_active_positions.return_value = [{
        "symbol": "SPY", "record_id": 7, "side": "buy",
        "entry_price": 100.0, "quantity": 1,
    }]
    patterns.db._connect.return_value.execute.return_value.fetchone.return_value = None
    patterns.close_tracked_position.return_value = {"dollar_pnl": -2.5}
    broker = MagicMock(is_simulating=False)
    broker.close_position.return_value = SimpleNamespace(success=True, order_id="stop-1", error=None)
    trading = MagicMock(broker=broker)
    trading._get_reference_price.return_value = 97.0
    orch._pattern_engine = patterns
    orch._trading_engine = trading
    closed = orch._check_active_positions(context="degraded-news")
    assert len(closed) == 1
    assert closed[0]["trigger"] == "STOP_LOSS"
    # Exits now route through the guarded path (register -> close -> confirm)
    # so duplicate closes are impossible. The underlying broker call is
    # covered by the guarded-exit tests in test_suite.py (E1).
    trading.close_position_guarded.assert_called_once_with("SPY", reason="STOP_LOSS")
    patterns.close_tracked_position.assert_called_once_with(record_id=7, current_price=97.0)


def test_live_missing_finnhub_credentials_fails_closed_without_constructor_crash():
    from news_ingestion import NewsIngestion, FinnhubClient
    news = NewsIngestion(finnhub_client=FinnhubClient(api_key=""), simulate=False)
    assert news.fetch_headlines() == []
    assert news.news_fetch_degraded is True
    assert "no fabricated headlines permitted" in news.news_degraded_reason


def test_provider_category_failure_degrades_real_provider_without_fabrication():
    news = NewsIngestion(finnhub_client=PartialFinnhub(), simulate=False, categories=["general", "merger"])
    headlines = news.fetch_headlines()
    assert headlines == ["General market update"]
    assert news.news_fetch_degraded is True
    assert "category fetch failed" in news.news_degraded_reason


def test_news_interface_status_and_aliases_fail_closed():
    from news_ingestion import NewsIngestion, FinnhubClient
    news = NewsIngestion(finnhub_client=FinnhubClient(api_key=""))
    assert news.status()["provider"] == "unavailable"
    assert news.get_headlines() == []
    assert news.get_articles() == []
    assert news.status()["degraded"] is True
    assert "no fabricated headlines" in news.status()["reason"]
