"""
LLM Cost Tracking for OpenLLMetry/Traceloop - Production Ready Version

This version handles multiple possible OpenTelemetry/Traceloop configurations
and provides detailed diagnostic information when initialization fails.

Key improvements over the basic version:
1. Multiple strategies for finding the span exporter
2. Detailed logging at each step of initialization
3. Graceful handling of different Traceloop versions
4. Clear error messages with troubleshooting guidance

Usage:
    from traceloop.sdk import Traceloop
    from llm_cost_injector import inject_llm_cost_tracking

    Traceloop.init(...)
    inject_llm_cost_tracking()
"""

import json
import logging
import requests
from typing import Dict, Optional, Any
from datetime import datetime, timedelta, timezone
from pathlib import Path
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult, BatchSpanProcessor
from opentelemetry.trace import StatusCode

logger = logging.getLogger(__name__)

# LiteLLM's community-maintained pricing database
LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)


class LiteLLMPricingDatabase:
    """
    Thread-safe pricing database synced from LiteLLM's GitHub repository.

    This database contains pricing for 1500+ models across 100+ providers.
    The data is cached locally to avoid network calls on every startup.
    """

    def __init__(self, cache_path: str = "./pricing_cache.json", sync_on_init: bool = True):
        self.cache_path = Path(cache_path)
        self.pricing: Dict[str, Dict[str, float]] = {}
        self.last_sync: Optional[datetime] = None

        # Try to load from cache first for fast startup
        if self.cache_path.exists():
            self._load_from_cache()

        # Sync from GitHub if cache is missing or stale
        if sync_on_init and (not self.pricing or self._is_cache_stale()):
            self.sync_pricing()

    def _load_from_cache(self):
        """Load pricing from local cache file."""
        try:
            with open(self.cache_path, 'r') as f:
                cache_data = json.load(f)
            self.pricing = cache_data.get('pricing', {})
            sync_time = cache_data.get('last_sync')
            self.last_sync = datetime.fromisoformat(sync_time) if sync_time else None
            logger.info(f"Loaded {len(self.pricing)} models from pricing cache")
        except Exception as e:
            logger.warning(f"Failed to load pricing cache: {e}")
            self.pricing = {}

    def _save_to_cache(self):
        """Save current pricing to cache file."""
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, 'w') as f:
                json.dump({
                    'pricing': self.pricing,
                    'last_sync': self.last_sync.isoformat() if self.last_sync else None
                }, f, indent=2)
            logger.info(f"Saved pricing cache with {len(self.pricing)} models")
        except Exception as e:
            logger.warning(f"Failed to save pricing cache: {e}")

    def _is_cache_stale(self, max_age_hours: int = 24) -> bool:
        """Check if cache needs refresh based on age."""
        if not self.last_sync:
            return True
        now = datetime.now(timezone.utc)
        last = self.last_sync if self.last_sync.tzinfo else self.last_sync.replace(tzinfo=timezone.utc)
        age = now - last
        return age > timedelta(hours=max_age_hours)

    def sync_pricing(self) -> bool:
        """
        Fetch latest pricing from LiteLLM's GitHub repository.

        This updates the pricing database with the latest community-maintained
        pricing data. The sync happens at startup if the cache is stale, and
        can also be triggered manually.

        Returns:
            True if sync succeeded, False otherwise
        """
        try:
            logger.info(f"Syncing pricing from LiteLLM GitHub...")
            response = requests.get(LITELLM_PRICING_URL, timeout=10)
            response.raise_for_status()

            litellm_data = response.json()
            logger.info(f"Downloaded pricing for {len(litellm_data)} models")

            # Transform LiteLLM's format to our simplified format
            new_pricing = {}
            for model_id, model_data in litellm_data.items():
                if 'input_cost_per_token' not in model_data:
                    continue  # Skip models without pricing info

                new_pricing[model_id] = {
                    'provider': model_data.get('litellm_provider', 'unknown'),
                    'input_per_million': model_data['input_cost_per_token'] * 1_000_000,
                    'output_per_million': model_data.get('output_cost_per_token', 0) * 1_000_000,
                    'cache_read_per_million': (
                        model_data.get('cache_read_input_token_cost', 0) * 1_000_000
                        if model_data.get('cache_read_input_token_cost') else None
                    ),
                }

            self.pricing = new_pricing
            self.last_sync = datetime.now(timezone.utc)
            self._save_to_cache()

            logger.info(f"✓ Pricing sync successful: {len(self.pricing)} models loaded")
            return True

        except Exception as e:
            logger.error(f"✗ Pricing sync failed: {e}")
            return False

    def _resolve_model_name(self, model: str) -> Optional[str]:
        """
        Resolve model name with fuzzy matching.

        Handles:
        - Provider prefixes: "openai/gpt-4o" -> "gpt-4o"
        - Versioned models: "gpt-4o-2024-08-06" -> "gpt-4o"
        - Exact matches
        """
        if not model:
            return None

        # Strip provider prefix (e.g., "openai/gpt-4o" -> "gpt-4o")
        if '/' in model:
            model = model.split('/')[-1]

        # Exact match check
        if model in self.pricing:
            return model

        # Fuzzy match for versioned models
        # e.g., "gpt-4o-2024-08-06" should match "gpt-4o"
        for known_model in self.pricing:
            if model.startswith(known_model) or known_model in model:
                return known_model

        return None

    def get_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0
    ) -> Dict[str, Any]:
        """
        Calculate cost for an LLM API call.

        Returns a dictionary suitable for setting as span attributes.
        All costs are in USD.
        """
        resolved_model = self._resolve_model_name(model)

        if not resolved_model or resolved_model not in self.pricing:
            # No pricing data available for this model
            logger.debug(f"No pricing data for model: {model}")
            return {
                'gen_ai.cost.input_usd': 0.0,
                'gen_ai.cost.output_usd': 0.0,
                'gen_ai.cost.total_usd': 0.0,
                'gen_ai.cost.model_resolved': model,
                'gen_ai.cost.provider': 'unknown',
                'gen_ai.cost.pricing_unavailable': True,
            }

        pricing = self.pricing[resolved_model]

        # Calculate regular input cost (excluding cached tokens)
        regular_input_tokens = input_tokens - cached_tokens
        input_cost = (regular_input_tokens / 1_000_000) * pricing['input_per_million']

        # Add cache read cost if applicable
        # Cached tokens are much cheaper (typically 10% of regular input cost)
        if cached_tokens > 0 and pricing.get('cache_read_per_million'):
            cache_cost = (cached_tokens / 1_000_000) * pricing['cache_read_per_million']
            input_cost += cache_cost

        # Calculate output cost
        # Output tokens typically cost 3-5x more than input tokens
        output_cost = (output_tokens / 1_000_000) * pricing['output_per_million']

        total_cost = input_cost + output_cost

        return {
            'gen_ai.cost.input_usd': round(input_cost, 8),
            'gen_ai.cost.output_usd': round(output_cost, 8),
            'gen_ai.cost.total_usd': round(total_cost, 8),
            'gen_ai.cost.model_resolved': resolved_model,
            'gen_ai.cost.provider': pricing['provider'],
            'gen_ai.cost.cached_tokens': cached_tokens,
        }


class CostEnrichingSpanExporter(SpanExporter):
    """
    Wrapper exporter that enriches LLM spans with cost data before export.

    This exporter wraps the original OTLP exporter and adds cost attributes
    to any span that contains LLM token usage data. The enrichment happens
    transparently without affecting the original exporter's behavior.
    """

    def __init__(self, wrapped_exporter: SpanExporter, pricing_db: LiteLLMPricingDatabase):
        self.wrapped_exporter = wrapped_exporter
        self.pricing_db = pricing_db

        # Track statistics for monitoring
        self.spans_enriched = 0
        self.spans_total = 0

    def export(self, spans) -> SpanExportResult:
        """
        Export spans after enriching them with cost data.

        This method is called by OpenTelemetry's BatchSpanProcessor when it's
        ready to export a batch of spans. We examine each span, identify LLM
        spans by checking for gen_ai.* attributes, calculate costs, and inject
        cost attributes before forwarding to the wrapped exporter.
        """
        self.spans_total += len(spans)

        # The pricing database might be None if initialization failed
        if self.pricing_db is None:
            logger.debug("Pricing database is None, skipping cost enrichment")
            return self.wrapped_exporter.export(spans)

        for span in spans:
            if self._is_llm_span(span):
                self._enrich_with_cost(span, self.pricing_db)
                self.spans_enriched += 1

        # Log statistics periodically (every 100 spans)
        if self.spans_total % 100 == 0:
            enrichment_rate = (self.spans_enriched / self.spans_total * 100) if self.spans_total > 0 else 0
            logger.debug(
                f"Cost enrichment stats: {self.spans_enriched}/{self.spans_total} spans "
                f"({enrichment_rate:.1f}% enrichment rate)"
            )

        # Forward to the wrapped exporter
        return self.wrapped_exporter.export(spans)

    def _is_llm_span(self, span: ReadableSpan) -> bool:
        """
        Check if a span represents an LLM API call.

        LLM spans are identified by the presence of gen_ai.system attribute,
        which is set by OpenTelemetry's LLM instrumentation packages.
        """
        attrs = span.attributes or {}
        return 'gen_ai.system' in attrs

    def _enrich_with_cost(self, span: ReadableSpan, pricing_db: LiteLLMPricingDatabase) -> ReadableSpan:
        """
        Add cost attributes to an LLM span.

        We extract token usage from the span's existing attributes (which were
        added by Traceloop's instrumentation), calculate costs using our pricing
        database, and inject the cost data back into the span's attributes.

        Note: We modify the span's internal _attributes dict directly. This is
        accessing a private implementation detail of the OpenTelemetry SDK, but
        it's the only way to add attributes to a span after it's been created.
        """
        try:
            attrs = dict(span.attributes or {})

            # Extract token usage from Traceloop's gen_ai.* attributes
            input_tokens = attrs.get('gen_ai.usage.input_tokens', 0)
            output_tokens = attrs.get('gen_ai.usage.output_tokens', 0)

            # Model can be in response.model or request.model
            model = attrs.get('gen_ai.response.model') or attrs.get('gen_ai.request.model')

            # Check for cached tokens (prompt caching feature)
            cached_tokens = attrs.get('gen_ai.usage.cache_read_tokens', 0)

            if not model or (input_tokens == 0 and output_tokens == 0):
                # No token usage data, skip cost calculation
                return span

            # Calculate costs using the pricing database
            cost_data = pricing_db.get_cost(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens
            )

            # Inject cost attributes into the span
            # This accesses the span's internal _attributes dict
            if hasattr(span, '_attributes'):
                span._attributes.update(cost_data)
                logger.debug(
                    f"Enriched span with cost: ${cost_data['gen_ai.cost.total_usd']:.6f} "
                    f"for {model} ({input_tokens}+{output_tokens} tokens)"
                )
            else:
                logger.warning("Span doesn't have _attributes dict - cannot enrich with cost")

            # Fix span status for OpenAI spans: the OpenAI instrumentor in
            # OpenLLMetry does not set span status to OK on success, so APM
            # records event.outcome as "unknown" instead of "success".
            # If we have a response model and token usage, the call succeeded.
            if (hasattr(span, '_status') and
                    span._status.status_code == StatusCode.UNSET and
                    attrs.get('gen_ai.response.model') and
                    output_tokens > 0):
                span._status = trace.Status(StatusCode.OK)

        except Exception as e:
            # Don't let cost enrichment errors break span export
            logger.error(f"Error enriching span with cost: {e}", exc_info=True)

        return span

    def shutdown(self):
        """Shutdown the wrapped exporter."""
        logger.info(
            f"Cost enriching exporter shutting down. "
            f"Enriched {self.spans_enriched} out of {self.spans_total} spans."
        )
        return self.wrapped_exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000):
        """Force flush the wrapped exporter."""
        return self.wrapped_exporter.force_flush(timeout_millis)


def _wrap_batch_processor(processor: BatchSpanProcessor, pricing_db: LiteLLMPricingDatabase) -> BatchSpanProcessor:
    """
    Create a new BatchSpanProcessor with a cost-enriching exporter wrapper.

    This preserves the original processor's configuration (queue sizes, delays, etc.)
    while wrapping the exporter to inject cost data into spans.

    Args:
        processor: Original BatchSpanProcessor to wrap
        pricing_db: Pricing database for cost calculation

    Returns:
        New BatchSpanProcessor with wrapped exporter
    """
    original_exporter = processor.span_exporter
    enriching_exporter = CostEnrichingSpanExporter(
        wrapped_exporter=original_exporter,
        pricing_db=pricing_db
    )

    return BatchSpanProcessor(
        span_exporter=enriching_exporter,
        max_queue_size=getattr(processor, '_max_queue_size', 2048),
        schedule_delay_millis=getattr(processor, '_schedule_delay_millis', 5000),
        max_export_batch_size=getattr(processor, '_max_export_batch_size', 512),
        export_timeout_millis=getattr(processor, '_export_timeout_millis', 30000)
    )


def _update_processor_list(obj, attr_name: str, processors_list: list) -> None:
    """
    Update a processor list attribute, trying tuple first then list format.

    OpenTelemetry may store processor lists as tuples (immutable) or lists.
    This function tries to maintain the original format.

    Args:
        obj: Object containing the processor list attribute
        attr_name: Name of the attribute to update
        processors_list: New list of processors to set
    """
    try:
        setattr(obj, attr_name, tuple(processors_list))
    except (AttributeError, TypeError):
        setattr(obj, attr_name, processors_list)


def _wrap_processor_list(processors_list: list, pricing_db: LiteLLMPricingDatabase, context: str = "processor") -> int:
    """
    Iterate through a list of processors and wrap any BatchSpanProcessors.

    This function modifies the processors_list in-place, replacing BatchSpanProcessors
    with new instances that have wrapped exporters.

    Args:
        processors_list: Mutable list of processors to modify in-place
        pricing_db: Pricing database for cost calculation
        context: Description for logging (e.g., "sub-processor", "processor")

    Returns:
        Number of processors successfully wrapped
    """
    wrapped_count = 0
    for i, processor in enumerate(processors_list):
        logger.info(f"    Checking {context} {i}: {type(processor).__name__}")

        if isinstance(processor, BatchSpanProcessor) and hasattr(processor, 'span_exporter'):
            try:
                new_processor = _wrap_batch_processor(processor, pricing_db)
                processors_list[i] = new_processor
                wrapped_count += 1
                logger.info(f"      ✓ Replaced {context} {i} with wrapped exporter")
            except Exception as e:
                logger.warning(f"      Failed to wrap {context} {i}: {e}")
        elif hasattr(processor, 'span_exporter'):
            logger.warning(
                f"      {context.capitalize()} {i} has span_exporter but is not a "
                f"BatchSpanProcessor: {type(processor).__name__}"
            )

    return wrapped_count


def _try_simple_exporter_wrap(processor, exporter_attr: str, pricing_db: LiteLLMPricingDatabase) -> bool:
    """
    Try to wrap a simple exporter attribute (span_exporter or _exporter).

    Some OpenTelemetry configurations use a simple exporter attribute that can be
    directly replaced. This is the simplest wrapping strategy.

    Args:
        processor: Processor object to check
        exporter_attr: Name of exporter attribute to check
        pricing_db: Pricing database for cost calculation

    Returns:
        True if successful, False otherwise
    """
    if hasattr(processor, exporter_attr):
        original_exporter = getattr(processor, exporter_attr)
        logger.info(f"  ✓ Found {exporter_attr}: {type(original_exporter).__name__}")

        enriching_exporter = CostEnrichingSpanExporter(
            wrapped_exporter=original_exporter,
            pricing_db=pricing_db
        )
        setattr(processor, exporter_attr, enriching_exporter)
        logger.info(f"  ✓ Successfully wrapped {exporter_attr}")
        logger.info("=" * 70)
        return True
    return False


def _find_and_wrap_exporter(tracer_provider: TracerProvider, pricing_db: LiteLLMPricingDatabase) -> bool:
    """
    Find the span exporter in the tracer provider and wrap it with cost enrichment.

    This function tries multiple strategies because different versions of Traceloop
    and OpenTelemetry organize their span processors differently. We check various
    possible locations and attribute names until we find the exporter.

    Returns:
        True if successfully wrapped an exporter, False otherwise
    """

    logger.info("=" * 70)
    logger.info("Searching for span exporter in tracer provider...")
    logger.info(f"Tracer provider type: {type(tracer_provider).__name__}")

    # Strategy 1: Check for single _active_span_processor with span_exporter
    # This is the most common structure in newer versions
    if hasattr(tracer_provider, '_active_span_processor'):
        processor = tracer_provider._active_span_processor
        logger.info(f"✓ Found _active_span_processor: {type(processor).__name__}")

        # Try the standard attribute name first
        if _try_simple_exporter_wrap(processor, 'span_exporter', pricing_db):
            return True

        # Try alternative attribute name with underscore prefix
        if _try_simple_exporter_wrap(processor, '_exporter', pricing_db):
            return True

        # Check if it's a composite processor with multiple child processors
        if hasattr(processor, '_span_processors'):
            processors_list = list(processor._span_processors)
            logger.info(f"  ✓ Found composite processor with {len(processors_list)} sub-processors")

            wrapped_count = _wrap_processor_list(processors_list, pricing_db, "sub-processor")

            if wrapped_count > 0:
                _update_processor_list(processor, '_span_processors', processors_list)
                logger.info(f"  ✓ Successfully wrapped {wrapped_count} exporter(s)")
                logger.info("=" * 70)
                return True

        # Log what attributes the processor actually has for debugging
        processor_attrs = [attr for attr in dir(processor) if not attr.startswith('__')]
        logger.warning(f"  Processor has these attributes: {processor_attrs}")

    # Strategy 2: Check for _span_processors list directly on tracer provider
    # Some configurations use a list of processors instead of a single active one
    if hasattr(tracer_provider, '_span_processors'):
        processors_list = list(tracer_provider._span_processors)
        logger.info(f"✓ Found _span_processors list with {len(processors_list)} processors")

        wrapped_count = _wrap_processor_list(processors_list, pricing_db, "processor")

        if wrapped_count > 0:
            _update_processor_list(tracer_provider, '_span_processors', processors_list)
            logger.info(f"✓ Successfully wrapped {wrapped_count} exporter(s)")
            logger.info("=" * 70)
            return True

    # If we get here, we couldn't find the exporter in any known location
    logger.error("=" * 70)
    logger.error("✗ FAILED TO FIND SPAN EXPORTER")
    logger.error("=" * 70)
    logger.error("Tracer provider structure doesn't match any known pattern.")
    logger.error("")
    logger.error("This could happen because:")
    logger.error("  1. You're using a newer/older version of Traceloop with different internals")
    logger.error("  2. Traceloop was initialized with custom configuration")
    logger.error("  3. OpenTelemetry SDK version incompatibility")
    logger.error("")
    logger.error("Diagnostic information:")
    logger.error(f"  Tracer provider type: {type(tracer_provider).__name__}")

    # Show what attributes are actually available for debugging
    provider_attrs = [attr for attr in dir(tracer_provider) if not attr.startswith('__')]
    logger.error(f"  Available attributes: {provider_attrs[:10]}...")  # Show first 10
    logger.error("")
    logger.error("Please contact support or file an issue with this diagnostic information.")
    logger.error("=" * 70)

    return False


def inject_llm_cost_tracking(
    cache_path: str = "./pricing_cache.json",
    sync_on_init: bool = True
):
    """
    Inject LLM cost tracking into Traceloop-instrumented application.

    Call this immediately after Traceloop.init() to automatically add cost
    tracking to all LLM API calls. The cost data will appear in Elastic APM
    as labels.gen_ai_cost_* attributes.

    Args:
        cache_path: Where to cache LiteLLM pricing data locally
        sync_on_init: Whether to sync pricing from GitHub if cache is stale

    Example:
        from traceloop.sdk import Traceloop
        from llm_cost_injector import inject_llm_cost_tracking

        Traceloop.init(app_name="my-app")
        inject_llm_cost_tracking()
    """
    logger.info("=" * 70)
    logger.info("INITIALIZING LLM COST TRACKING")
    logger.info("=" * 70)

    # Step 1: Initialize pricing database
    logger.info("Step 1: Initializing pricing database...")
    pricing_db = LiteLLMPricingDatabase(
        cache_path=cache_path,
        sync_on_init=sync_on_init
    )
    logger.info(f"  ✓ Pricing database ready with {len(pricing_db.pricing)} models")

    # Step 2: Get tracer provider
    logger.info("Step 2: Retrieving tracer provider...")
    tracer_provider = trace.get_tracer_provider()
    logger.info(f"  ✓ Got tracer provider: {type(tracer_provider).__name__}")

    # Step 3: Find and wrap the exporter
    logger.info("Step 3: Finding and wrapping span exporter...")
    success = _find_and_wrap_exporter(tracer_provider, pricing_db)

    if success:
        logger.info("")
        logger.info("=" * 70)
        logger.info("✓✓✓ LLM COST TRACKING SUCCESSFULLY ENABLED ✓✓✓")
        logger.info("=" * 70)
        logger.info(f"Loaded pricing for {len(pricing_db.pricing)} models")
        logger.info("All LLM API calls will now include cost data in traces")
        logger.info("Cost attributes will appear in Elastic APM as:")
        logger.info("  - labels.gen_ai_cost_total_usd")
        logger.info("  - labels.gen_ai_cost_input_usd")
        logger.info("  - labels.gen_ai_cost_output_usd")
        logger.info("  - labels.gen_ai_cost_provider")
        logger.info("  - labels.gen_ai_cost_model_resolved")
        logger.info("=" * 70)
    else:
        logger.error("")
        logger.error("=" * 70)
        logger.error("✗✗✗ LLM COST TRACKING NOT ENABLED ✗✗✗")
        logger.error("=" * 70)
        logger.error("Could not find span exporter to wrap.")
        logger.error("Your traces will NOT include cost data.")
        logger.error("")
        logger.error("To fix this:")
        logger.error("  1. Check that Traceloop.init() was called before inject_llm_cost_tracking()")
        logger.error("  2. Verify you're using a compatible version of Traceloop")
        logger.error("  3. Review the diagnostic information above")
        logger.error("=" * 70)


def sync_pricing_database(cache_path: str = "./pricing_cache.json"):
    """
    Manually trigger a pricing database sync from LiteLLM.

    Use this to refresh pricing after you know a provider has changed rates.
    """
    db = LiteLLMPricingDatabase(cache_path=cache_path, sync_on_init=False)
    success = db.sync_pricing()
    if success:
        logger.info(f"Pricing sync successful: {len(db.pricing)} models")
    else:
        logger.error("Pricing sync failed")
    return success
