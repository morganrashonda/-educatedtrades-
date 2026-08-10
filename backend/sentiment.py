"""
Sentiment Analysis Engine for Educated Trades.

Combines VADER (rule-based sentiment) with custom market-specific keyword
weights to produce a conviction score from news headlines.

Architecture:
  MarketSentimentEngine
    │
    ├── VADER base sentiment (compound score: -1 to +1)
    ├── Custom market keyword weights (boosted/penalized terms)
    ├── Headline-level conviction scoring
    └── Aggregate conviction over a batch of headlines

Usage:
    from sentiment import MarketSentimentEngine

    engine = MarketSentimentEngine()
    result = engine.analyze(["Fed raises rates by 25bps", "Tax cuts proposed"])
    print(result["conviction_score"])   # e.g. 0.65
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# VADER import
# ---------------------------------------------------------------------------
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    _VADER_AVAILABLE = True
except ImportError:
    _VADER_AVAILABLE = False
    SentimentIntensityAnalyzer = None  # type: ignore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom market keyword weights
# ---------------------------------------------------------------------------
# These weights augment / override VADER's default lexicon for market-specific
# terms.  Positive weights indicate bullish sentiment, negative = bearish.
# The magnitude reflects the term's expected market impact intensity.

MARKET_KEYWORD_WEIGHTS: Dict[str, float] = {
    # Emptied deliberately. This held 77 hand-assigned weights that
    # nobody had ever measured against returns -- and two substring
    # matches in a headline were enough to trigger a trade. On
    # SPY/QQQ/IWM, headline sentiment is not a defensible signal at
    # all: broad index ETFs reprice public news in seconds.
    #
    # Left as an empty table rather than deleted so the engine still
    # imports and scores 0.0 -- neutral and inert -- instead of
    # failing at startup for anything that still references it.
}


# ---------------------------------------------------------------------------
# Negation handling (precision refinement)
# ---------------------------------------------------------------------------
# Substring keyword matching is blind to negation: "company AVOIDS layoffs"
# or "recession fears EASE" previously fired the bearish keyword at full
# strength, manufacturing false short signals. When a negation cue appears
# within NEGATION_WINDOW tokens before a keyword, we flip its sign and damp
# it by NEGATION_DAMPING (a negated bearish phrase is only mildly bullish).

# Cues that PRECEDE the keyword ("no recession", "avoids layoffs",
# "unlikely to cut rates", "rules out a hike").
NEGATION_CUES = frozenset({
    "no", "not", "never", "without", "avoids", "avoid", "avoided",
    "avoiding", "downplay", "downplays", "downplayed", "unlikely",
    "denies", "denied", "deny", "dismiss", "dismisses", "dismissed",
    "rules", "rule", "ruled",  # "rules out", "ruled out"
    "isnt", "arent", "wasnt", "werent", "wont", "doesnt", "dont", "didnt",
})
# Cues that typically FOLLOW the keyword and reverse a risk/threat word,
# e.g. "recession fears EASE", "tariff worries FADE", "selloff REVERSES".
REVERSAL_CUES_FORWARD = frozenset({
    "ease", "eases", "easing", "eased",
    "fade", "fades", "fading", "faded",
    "recede", "recedes", "receding", "receded",
    "reverse", "reverses", "reversed", "reversing",
    "subside", "subsides", "subsided",
    "abate", "abates", "abated",
})
NEGATION_WINDOW = 3        # tokens before the keyword to scan for a cue
FORWARD_WINDOW = 3         # tokens after the keyword to scan for a reversal cue
NEGATION_DAMPING = 0.4     # keep negated terms well below full strength


# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------
@dataclass
class HeadlineResult:
    """Scored result for a single headline."""

    headline: str
    vader_compound: float
    market_adjustment: float
    conviction_score: float
    matched_keywords: Dict[str, float] = field(default_factory=dict)
    confidence: str = "low"

    @property
    def sentiment_label(self) -> str:
        if self.conviction_score >= 0.35:
            return "bullish"
        elif self.conviction_score <= -0.35:
            return "bearish"
        return "neutral"


@dataclass
class BatchResult:
    """Aggregated result across a batch of headlines."""

    headlines: List[HeadlineResult]
    aggregate_conviction: float
    consensus: str
    headline_count: int
    volatility_signal: float  # std of headline scores — higher = more divergence


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class MarketSentimentEngine:
    """
    Sentiment analysis engine that blends VADER with custom market keyword
    weights to produce a conviction score for financial news headlines.
    """

    def __init__(
        self,
        keyword_weights: Optional[Dict[str, float]] = None,
        vader_threshold: float = 0.05,
    ):
        """
        Args:
            keyword_weights: Custom market keyword weights (merged with defaults).
            vader_threshold: Vader compound scores below this magnitude are
                             considered neutral before market adjustment.
        """
        # Merge default weights with any overrides provided by user
        self.keyword_weights = dict(MARKET_KEYWORD_WEIGHTS)
        if keyword_weights:
            self.keyword_weights.update(keyword_weights)

        self.vader_threshold = vader_threshold

        # Initialise VADER
        if _VADER_AVAILABLE:
            self._vader = SentimentIntensityAnalyzer()
            # Boost VADER's lexicon with our custom keyword weights
            self._boost_vader_lexicon()
        else:
            self._vader = None
            logger.warning(
                "VADER not available; falling back to keyword-only scoring."
            )

        logger.debug(
            "MarketSentimentEngine initialised with %d custom keyword weights.",
            len(self.keyword_weights),
        )

    # ------------------------------------------------------------------
    # Lexicon boosting
    # ------------------------------------------------------------------
    def _boost_vader_lexicon(self) -> None:
        """Inject our custom keyword weights into VADER's internal lexicon."""
        if not self._vader:
            return
        for word, weight in self.keyword_weights.items():
            # Normalise: keep only alphabetic characters, lowercase
            normalised = re.sub(r"[^a-z\s]", "", word.lower()).strip()
            if normalised:
                self._vader.lexicon[normalised] = weight

    # ------------------------------------------------------------------
    # Keyword matching
    # ------------------------------------------------------------------
    def _find_market_keywords(self, text: str) -> Dict[str, float]:
        """
        Find all custom market keywords present in the text and return
        them with their (possibly negation-adjusted) weights.

        Negation handling (precision fix): a keyword preceded within a short
        window by a negation cue ("no", "not", "avoids", "eases", "downplay",
        "unlikely", etc.) has its weight flipped and damped. Raw substring
        matching previously fired full-strength on phrases like
        "company AVOIDS layoffs" or "recession fears EASE", manufacturing
        false bearish signals — the dominant source of losing short trades.
        """
        text_lower = text.lower()
        # Tokenise once for negation-window lookups (keep word order).
        tokens = re.findall(r"[a-z]+(?:-[a-z]+)*", text_lower)

        matched: Dict[str, float] = {}
        for keyword, weight in self.keyword_weights.items():
            kw_lower = keyword.lower()
            if kw_lower not in text_lower:
                continue

            if self._is_negated(kw_lower, tokens, weight):
                # Flip and damp: a negated bearish term is mildly bullish and
                # vice-versa, but we never let a negated term carry full force.
                matched[keyword] = round(-weight * NEGATION_DAMPING, 4)
            else:
                matched[keyword] = weight
        return matched

    @staticmethod
    def _is_negated(keyword: str, tokens: List[str], weight: float = 0.0) -> bool:
        """
        Return True if ``keyword`` should be treated as negated, i.e.:
          - a negation cue appears in the NEGATION_WINDOW tokens preceding it
            (e.g. "no recession", "avoids layoffs"), OR
          - it is a NEGATIVE (risk/threat) keyword followed within
            FORWARD_WINDOW tokens by a reversal cue ("recession fears ease",
            "tariff worries fade").
        """
        words = keyword.split()
        first_word, last_word = words[0], words[-1]
        for i, tok in enumerate(tokens):
            if tok == first_word:
                # Backward negation window.
                back = tokens[max(0, i - NEGATION_WINDOW):i]
                if any(cue in NEGATION_CUES for cue in back):
                    return True
                # Forward reversal window — only meaningful for risk words
                # (negative weight); reversal cues shouldn't flip good news.
                if weight < 0:
                    fwd_start = i + len(words)
                    fwd = tokens[fwd_start:fwd_start + FORWARD_WINDOW]
                    # also scan a couple tokens after the keyword's last word
                    if any(cue in REVERSAL_CUES_FORWARD for cue in fwd):
                        return True
        return False

    def _calculate_market_adjustment(self, matched: Dict[str, float]) -> float:
        """
        Aggregate keyword weights into a single market adjustment value.
        Uses a sum with diminishing returns for multiple keywords —
        the second keyword adds 70% of its weight, the third 50%, etc.
        """
        if not matched:
            return 0.0

        # Rank by magnitude BEFORE applying decay. The decay was previously
        # applied in dict-iteration order, which is the order keywords happen
        # to be defined in keyword_weights -- so whether a strong signal
        # counted fully or got damped depended on where someone typed it in
        # the table. The same two keywords scored -3.12 or -2.19 purely from
        # ordering. Strongest signal counts fully; extras diminish.
        # Sort on (magnitude, sign) so the ordering is TOTAL. Sorting on
        # magnitude alone leaves ties -- e.g. -3.5 and +3.5 -- broken by
        # Python's stable sort, i.e. by insertion order again, which is the
        # very non-determinism this is meant to remove.
        weights = sorted(matched.values(), key=lambda w: (-abs(w), -w))
        total = 0.0
        for i, w in enumerate(weights):
            decay = 1.0 / (1.0 + 0.3 * i)
            total += w * decay

        # Clamp to [-5, +5] range
        return max(-5.0, min(5.0, total))

    # ------------------------------------------------------------------
    # Conviction blending
    # ------------------------------------------------------------------
    def _blend_conviction(
        self,
        vader_compound: float,
        market_adjust: float,
        vader_confidence: float,
    ) -> float:
        """
        Blend VADER compound score and market adjustment into a final
        conviction score in the range [-1, +1].

        Strategy:
          - If VADER gives a confident reading (|compound| > threshold),
            we blend: 0.6 * vader_compound + 0.4 * (market_adjust / 5)
          - If VADER is neutral but keywords exist, bias towards keywords.
          - If both are neutral → 0.
        """
        # Normalise market adjustment to [-1, +1]
        market_normalised = market_adjust / 5.0

        if abs(vader_compound) > self.vader_threshold and vader_confidence > 0.4:
            # Confident VADER reading → blend
            blended = 0.6 * vader_compound + 0.4 * market_normalised
        elif abs(market_normalised) > 0.2:
            # Keywords provide signal even when VADER is undecided
            blended = 0.3 * vader_compound + 0.7 * market_normalised
        else:
            blended = vader_compound

        # Clamp to [-1, +1]
        return max(-1.0, min(1.0, round(blended, 4)))

    @staticmethod
    def _compute_confidence(
        vader_compound: float, vader_scores: dict, keyword_count: int
    ) -> str:
        """
        Determine confidence level based on signal strength and volume.
        """
        magnitude = abs(vader_compound)
        has_positive = vader_scores.get("pos", 0) > 0.1
        has_negative = vader_scores.get("neg", 0) > 0.1
        has_keywords = keyword_count > 0

        if magnitude > 0.5 and (has_positive or has_negative) and has_keywords:
            return "high"
        elif magnitude > 0.3 or has_keywords:
            return "medium"
        return "low"

    # ------------------------------------------------------------------
    # Single headline analysis
    # ------------------------------------------------------------------
    def analyze_headline(self, headline: str) -> HeadlineResult:
        """
        Analyze a single news headline and return a structured result.
        """
        # 1. VADER scoring
        if self._vader:
            vader_scores = self._vader.polarity_scores(headline)
            vader_compound = vader_scores["compound"]
        else:
            vader_scores = {"compound": 0.0, "pos": 0.0, "neg": 0.0, "neu": 1.0}
            vader_compound = 0.0

        # 2. Custom market keyword matching
        matched = self._find_market_keywords(headline)
        market_adjust = self._calculate_market_adjustment(matched)

        # 3. Confidence
        confidence = self._compute_confidence(
            vader_compound, vader_scores, len(matched)
        )

        # 4. Blend into conviction score
        vader_conf_score = abs(vader_scores.get("pos", 0) - vader_scores.get("neg", 0))
        conviction = self._blend_conviction(vader_compound, market_adjust, vader_conf_score)

        return HeadlineResult(
            headline=headline,
            vader_compound=vader_compound,
            market_adjustment=round(market_adjust, 4),
            conviction_score=conviction,
            matched_keywords=matched,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Batch analysis
    # ------------------------------------------------------------------
    def analyze(
        self, headlines: List[str]
    ) -> BatchResult:
        """
        Analyze a batch of headlines and produce an aggregate conviction score.

        Args:
            headlines: List of news headline strings.

        Returns:
            BatchResult with per-headline scores and aggregated conviction.
        """
        if not headlines:
            return BatchResult(
                headlines=[],
                aggregate_conviction=0.0,
                consensus="neutral",
                headline_count=0,
                volatility_signal=0.0,
            )

        results = [self.analyze_headline(h) for h in headlines]

        # Weighted aggregate: headlines with higher conviction confidence
        # contribute more to the aggregate score.
        total_weight = 0.0
        weighted_sum = 0.0

        confidence_multipliers = {"high": 1.0, "medium": 0.6, "low": 0.2}

        for r in results:
            w = confidence_multipliers.get(r.confidence, 0.3)
            weighted_sum += r.conviction_score * w
            total_weight += w

        aggregate = round(weighted_sum / total_weight, 4) if total_weight > 0 else 0.0

        # Volatility signal: standard deviation of conviction scores
        if len(results) > 1:
            scores = [r.conviction_score for r in results]
            mean = sum(scores) / len(scores)
            variance = sum((s - mean) ** 2 for s in scores) / len(scores)
            volatility = round(variance**0.5, 4)
        else:
            volatility = 0.0

        # Consensus label
        if aggregate >= 0.35:
            consensus = "bullish"
        elif aggregate <= -0.35:
            consensus = "bearish"
        else:
            consensus = "neutral"

        return BatchResult(
            headlines=results,
            aggregate_conviction=aggregate,
            consensus=consensus,
            headline_count=len(results),
            volatility_signal=volatility,
        )

    # ------------------------------------------------------------------
    # Mock news source (for development / testing)
    # ------------------------------------------------------------------
    @staticmethod
    def get_mock_headlines() -> List[str]:
        """
        Returns a set of synthetic financial news headlines for testing
        and development when no live API feed is connected.
        """
        return [
            "Fed holds interest rates steady, signals cautious approach to easing",
            "Treasury yields spike as inflation data comes in hotter than expected",
            "Tech stocks rally on earnings beat from major semiconductor firm",
            "Trade tensions escalate as new tariffs announced on imported steel",
            "Jobless claims fall to 50-year low, labour market remains tight",
            "Oil prices surge on supply chain disruption in Middle East",
            "Congress passes infrastructure spending bill worth $1.2 trillion",
            "Retail giant misses earnings targets, cites consumer spending slowdown",
            "Dovish Fed minutes boost market sentiment across all sectors",
            "Bear market fears resurface as volatility index climbs sharply",
            "Corporate tax cuts proposed in new economic stimulus package",
            "Supply chain recovery underway as shipping costs decline for third month",
        ]


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------
def quick_conviction(headlines: List[str], **kwargs) -> float:
    """
    One-shot convenience function: analyze headlines and return the
    aggregate conviction score.
    """
    engine = MarketSentimentEngine(**kwargs)
    return engine.analyze(headlines).aggregate_conviction


def quick_batch(headlines: List[str], **kwargs) -> dict:
    """
    One-shot convenience function: analyze headlines and return a
    dictionary with key results (suitable for API serialisation).
    """
    engine = MarketSentimentEngine(**kwargs)
    result = engine.analyze(headlines)
    return {
        "conviction_score": result.aggregate_conviction,
        "consensus": result.consensus,
        "volatility_signal": result.volatility_signal,
        "headline_count": result.headline_count,
        "headlines": [
            {
                "text": h.headline,
                "conviction": h.conviction_score,
                "label": h.sentiment_label,
                "confidence": h.confidence,
                "matched_keywords": list(h.matched_keywords.keys()),
            }
            for h in result.headlines
        ],
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys

    is_json = "--json" in sys.argv
    log_level = logging.WARNING if is_json else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s %(message)s")

    engine = MarketSentimentEngine()

    # Use provided headlines from argv or fall back to mock data
    if len(sys.argv) > 1 and not (len(sys.argv) == 2 and sys.argv[1] == "--json"):
        # Filter out --json from headlines if it's there
        headlines = [arg for arg in sys.argv[1:] if arg != "--json"]
        source = "CLI args"
    else:
        headlines = engine.get_mock_headlines()
        source = "mock data"

    if not is_json:
        print(f"\n{'='*60}")
        print(f"  Educated Trades — Sentiment Analysis Engine")
        print(f"  Source: {source} ({len(headlines)} headlines)")
        print(f"{'='*60}")

    result = engine.analyze(headlines)

    if not is_json:
        print(f"\n  Aggregate Conviction : {result.aggregate_conviction:+.4f}")
        print(f"  Consensus            : {result.consensus.upper()}")
        print(f"  Volatility Signal    : {result.volatility_signal:.4f}")
        print(f"{'='*60}\n")

        for h in result.headlines:
            kw = list(h.matched_keywords.keys())
            kw_str = f"  keywords={kw}" if kw else ""
            print(
                f"  {h.conviction_score:+.4f} ({h.confidence:>6s})  "
                f"{h.headline[:80]}{'...' if len(h.headline) > 80 else ''}"
                f"{kw_str}"
            )

        print(f"\n{'='*60}\n")

    # Also print JSON for programmatic consumption
    if is_json:
        print(json.dumps(quick_batch(headlines)))
        sys.exit(0)

    print("--- JSON OUTPUT ---")
    print(json.dumps(quick_batch(headlines), indent=2))
