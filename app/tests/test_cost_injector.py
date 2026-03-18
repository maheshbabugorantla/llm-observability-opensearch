"""Tests for CostEnrichingSpanExporter and LiteLLMPricingDatabase."""

from unittest.mock import MagicMock

import pytest
from llm_cost_injector import CostEnrichingSpanExporter, LiteLLMPricingDatabase


@pytest.fixture
def pricing_db():
    """Create a pricing database with known test data (no network calls)."""
    db = LiteLLMPricingDatabase(cache_path="/tmp/test_pricing_cache.json", sync_on_init=False)
    db.pricing = {
        "gpt-4o": {
            "provider": "openai",
            "input_per_million": 2.50,
            "output_per_million": 10.00,
            "cache_read_per_million": None,
        },
        "claude-3-5-sonnet-20241022": {
            "provider": "anthropic",
            "input_per_million": 3.00,
            "output_per_million": 15.00,
            "cache_read_per_million": 0.30,
        },
    }
    return db


@pytest.fixture
def mock_exporter():
    """Create a mock OTLP span exporter."""
    exporter = MagicMock(spec=["export", "shutdown", "force_flush"])
    exporter.export.return_value = None
    return exporter


def _make_span(attributes):
    """Create a mock span with given attributes."""
    span = MagicMock()
    span.attributes = attributes
    span._attributes = dict(attributes)
    return span


class TestLiteLLMPricingDatabase:
    def test_llm_span_gets_cost_attributes(self, pricing_db):
        """Span with gen_ai.system should get cost.* attributes injected."""
        cost = pricing_db.get_cost("gpt-4o", input_tokens=1000, output_tokens=500)
        assert cost["gen_ai.cost.input_usd"] > 0
        assert cost["gen_ai.cost.output_usd"] > 0
        assert cost["gen_ai.cost.total_usd"] == pytest.approx(
            cost["gen_ai.cost.input_usd"] + cost["gen_ai.cost.output_usd"]
        )
        assert cost["gen_ai.cost.provider"] == "openai"
        assert cost["gen_ai.cost.model_resolved"] == "gpt-4o"

    def test_non_llm_span_passes_through(self, pricing_db):
        """Span without gen_ai.system should not be modified."""
        cost = pricing_db.get_cost(None, input_tokens=0, output_tokens=0)
        assert cost["gen_ai.cost.total_usd"] == 0.0
        assert cost["gen_ai.cost.pricing_unavailable"] is True

    def test_missing_tokens_default_to_zero_cost(self, pricing_db):
        """Missing input_tokens/output_tokens should produce $0 cost."""
        cost = pricing_db.get_cost("gpt-4o", input_tokens=0, output_tokens=0)
        assert cost["gen_ai.cost.total_usd"] == 0.0
        assert cost["gen_ai.cost.input_usd"] == 0.0
        assert cost["gen_ai.cost.output_usd"] == 0.0

    def test_model_fuzzy_matching(self, pricing_db):
        """gpt-4o-2024-08-06 should resolve to gpt-4o pricing."""
        cost = pricing_db.get_cost("gpt-4o-2024-08-06", input_tokens=1000, output_tokens=500)
        assert cost["gen_ai.cost.model_resolved"] == "gpt-4o"
        assert cost["gen_ai.cost.total_usd"] > 0

    def test_provider_prefix_stripping(self, pricing_db):
        """openai/gpt-4o should resolve to gpt-4o."""
        cost = pricing_db.get_cost("openai/gpt-4o", input_tokens=1000, output_tokens=500)
        assert cost["gen_ai.cost.model_resolved"] == "gpt-4o"
        assert cost["gen_ai.cost.total_usd"] > 0

    def test_unknown_model_handled_gracefully(self, pricing_db):
        """Unknown model name should not raise, should return zero cost with flag."""
        cost = pricing_db.get_cost("totally-unknown-model-xyz", input_tokens=100, output_tokens=50)
        assert cost["gen_ai.cost.total_usd"] == 0.0
        assert cost["gen_ai.cost.pricing_unavailable"] is True

    def test_cost_calculation_accuracy(self, pricing_db):
        """Verify cost math: 1M input tokens of gpt-4o at $2.50/M = $2.50."""
        cost = pricing_db.get_cost("gpt-4o", input_tokens=1_000_000, output_tokens=0)
        assert cost["gen_ai.cost.input_usd"] == pytest.approx(2.50)
        assert cost["gen_ai.cost.output_usd"] == 0.0
        assert cost["gen_ai.cost.total_usd"] == pytest.approx(2.50)


class TestCostEnrichingSpanExporter:
    def test_enriches_llm_spans(self, mock_exporter, pricing_db):
        """LLM spans should have cost attributes injected."""
        wrapper = CostEnrichingSpanExporter(mock_exporter, pricing_db)
        span = _make_span({
            "gen_ai.system": "openai",
            "gen_ai.response.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 500,
            "gen_ai.usage.output_tokens": 200,
        })
        wrapper.export([span])
        mock_exporter.export.assert_called_once()
        assert "gen_ai.cost.total_usd" in span._attributes

    def test_non_llm_spans_pass_through(self, mock_exporter, pricing_db):
        """Non-LLM spans should not be modified."""
        wrapper = CostEnrichingSpanExporter(mock_exporter, pricing_db)
        span = _make_span({"http.method": "GET", "http.url": "/health"})
        original_attrs = dict(span._attributes)
        wrapper.export([span])
        mock_exporter.export.assert_called_once()
        assert span._attributes == original_attrs
