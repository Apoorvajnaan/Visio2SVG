#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
General SEO keyword ranking module for agent pipelines.

Usage:
- Your agent parses documents and web sources to produce a list of keyword candidates.
- Each candidate is a dict with optional fields (see KeywordCandidate below).
- Call rank_keywords(candidates, cfg) to get primary, secondary, and long-tail sets,
  plus a JSON-ready payload for downstream CMS/SEO steps.
"""

from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Tuple, Optional
from collections import defaultdict, Counter
import math
import re

# ----------------------------
# Data models and configuration
# ----------------------------

@dataclass
class KeywordCandidate:
    term: str
    source: str = "web"                  # e.g., "document" | "web" | "enterprise"
    lang: str = "en"
    intent: str = "informational"        # "informational" | "transactional" | "navigational" | "technical"
    region: Optional[str] = None         # e.g., "US", "DE", "JP"

    # Document-derived signals (0..1 preferred). Agent should populate if available.
    doc_relevance: Optional[float] = None     # Importance within document (e.g., normalized TF-IDF/BM25 or embedding similarity)
    entity_presence: Optional[float] = None   # Strength of associated entities (product names, standards, interfaces), 0..1
    specificity: Optional[float] = None       # Domain specificity (0..1); if unknown, heuristics apply

    # Web/market metrics (raw; normalization handled here)
    volume: Optional[float] = None            # Monthly search volume (absolute or comparable scale)
    difficulty: Optional[float] = None        # Competition/difficulty; supports 0..1 or 0..100 scales
    trend: Optional[float] = None             # Growth trend; -1..1 (declining..growing) or 0..1 if already normalized
    serp_openings: Optional[float] = None     # Likelihood of content winning (SERP format/openness), 0..1
    competitor_strength: Optional[float] = None  # 0..1 (higher means tougher competition)
    brand_fit: Optional[float] = None         # 0..1 alignment with your portfolio and messaging

@dataclass
class RankedKeyword:
    term: str
    intent: str
    region: Optional[str]
    final_score: float

    # Breakdown
    relevance_score: float
    market_score: float
    opportunity_score: float

    # Normalized/derived fields
    volume_norm: float
    difficulty_norm: float
    trend_norm: float
    specificity: float
    entity_presence: float
    doc_relevance: float
    serp_openings: float
    competitor_gap: float
    brand_fit: float
    length_tokens: int
    notes: str = ""

@dataclass
class KeywordRankConfig:
    """
    Weight configuration is domain-agnostic. Adjust alpha/beta/gamma
    to emphasize relevance (alpha), market (beta), or opportunity (gamma).
    """
    # Component weights within relevance, market, opportunity
    w_relevance: Tuple[float, float, float, float, float] = (0.28, 0.18, 0.12, 0.22, 0.20)  # sem, bm25, entity, intent, specificity
    v_market: Tuple[float, float, float] = (0.50, 0.30, 0.20)  # volume, (1 - difficulty), trend
    u_opportunity: Tuple[float, float, float] = (0.45, 0.30, 0.25)  # competitor_gap, serp_openings, brand_fit

    # Final blending weights
    alpha_beta_gamma: Tuple[float, float, float] = (0.45, 0.30, 0.25)

    # Target content type informs intent alignment scoring
    target_content_type: str = "TechArticle"   # or "Product"

    # Selection thresholds
    min_relevance: float = 0.45
    min_specificity: float = 0.50
    min_volume_norm: float = 0.10

    # Output sizes
    primary_top_n: int = 10
    secondary_top_n: int = 15
    long_tail_max_n: int = 20

    # Optional regional weighting; e.g., {"US": 1.0, "DE": 0.8}
    region_weights: Optional[Dict[str, float]] = None

    # Optional generic term penalties (empty tuple disables)
    generic_penalties: Tuple[str, ...] = ()

    # Optional regex patterns for boosting specificity if present (empty tuple disables)
    boost_patterns: Tuple[re.Pattern, ...] = ()

# ----------------------------
# Utility functions
# ----------------------------

def _safe(x: Optional[float], default: float) -> float:
    try:
        if x is None or math.isnan(x):
            return default
        return float(x)
    except Exception:
        return default

def _min_max_norm(values: List[Optional[float]]) -> List[float]:
    vals = [v for v in values if v is not None]
    if not vals:
        return [0.0 for _ in values]
    vmin, vmax = min(vals), max(vals)
    if vmax == vmin:
        # If constant, assign mid value to knowns, 0 for missing
        return [0.5 if v is not None else 0.0 for v in values]
    return [0.0 if v is None else (v - vmin) / (vmax - vmin) for v in values]

def _difficulty_norm(raw: Optional[float]) -> float:
    # Accept 0..1 or 0..100; clamp to 0..1
    if raw is None:
        return 0.5
    if raw > 1.0:
        return max(0.0, min(1.0, raw / 100.0))
    return max(0.0, min(1.0, raw))

def _trend_norm(raw: Optional[float]) -> float:
    # If provided as -1..1, map to 0..1; if 0..1, keep
    if raw is None:
        return 0.5
    if -1.0 <= raw <= 1.0:
        return max(0.0, min(1.0, (raw + 1.0) / 2.0))
    return max(0.0, min(1.0, raw))

def _intent_alignment(intent: str, target: str) -> float:
    intent = (intent or "informational").lower()
    target = (target or "TechArticle").lower()
    if target == "product":
        order = {"transactional": 1.0, "navigational": 0.9, "informational": 0.7, "technical": 0.8}
    else:
        order = {"informational": 1.0, "technical": 0.95, "navigational": 0.7, "transactional": 0.6}
    return order.get(intent, 0.6)

def _specificity_heuristic(term: str, base: float, cfg: KeywordRankConfig) -> float:
    """
    Domain-agnostic specificity with optional configurable boosts.
    - Adds a small boost for longer, more precise phrases.
    - Applies optional regex boosts (if provided by caller) for domain markers.
    - Applies optional penalties for overly generic terms.
    """
    tokens = term.split()
    boost = 0.02 * min(len(tokens), 10)  # modest reward for multi-word specificity
    if cfg.boost_patterns:
        if any(p.search(term) for p in cfg.boost_patterns):
            boost += 0.10
    if cfg.generic_penalties and any(g in term.lower() for g in cfg.generic_penalties):
        boost -= 0.10
    return max(0.0, min(1.0, base + boost))

def _entity_presence_default(term: str, val: Optional[float]) -> float:
    if val is not None:
        return max(0.0, min(1.0, val))
    # Heuristic: more tokens => likely more concrete phrase
    return max(0.0, min(1.0, 0.3 + 0.05 * min(len(term.split()), 8)))

def _serp_openings_default(val: Optional[float]) -> float:
    return max(0.0, min(1.0, 0.6 if val is None else val))

def _brand_fit_default(val: Optional[float]) -> float:
    return max(0.0, min(1.0, 0.8 if val is None else val))

def _competitor_gap_from_strength(strength: Optional[float]) -> float:
    # Gap = 1 - strength; missing => medium competition
    if strength is None:
        return 0.5
    s = max(0.0, min(1.0, strength))
    return 1.0 - s

def _semantic_proxy(candidate: KeywordCandidate) -> float:
    # Use doc_relevance if provided; else fallback to a neutral heuristic
    if candidate.doc_relevance is not None:
        return max(0.0, min(1.0, candidate.doc_relevance))
    base = 0.6 + 0.05 * min(len(candidate.term.split()), 8)
    return max(0.0, min(1.0, base))

def _bm25_proxy(candidate: KeywordCandidate) -> float:
    # Fallback to doc_relevance proxy if BM25/TF-IDF not provided
    return _semantic_proxy(candidate)

# ----------------------------
# Core ranking pipeline
# ----------------------------

def deduplicate_candidates(cands: List[KeywordCandidate]) -> List[KeywordCandidate]:
    """
    Merge duplicates (case/space-insensitive) by averaging numeric signals and majority voting on intent.
    """
    bucket: Dict[str, List[KeywordCandidate]] = defaultdict(list)
    for c in cands:
        key = re.sub(r"\s+", " ", c.term.strip().lower())
        bucket[key].append(c)
    merged: List[KeywordCandidate] = []
    for key, items in bucket.items():
        merged_item = KeywordCandidate(term=items[0].term)
        # Merge numeric fields by average
        num_fields = ("doc_relevance", "entity_presence", "specificity", "volume",
                      "difficulty", "trend", "serp_openings", "competitor_strength", "brand_fit")
        for f in num_fields:
            vals = [getattr(i, f) for i in items if getattr(i, f) is not None]
            if vals:
                setattr(merged_item, f, sum(vals) / len(vals))
        # Merge categorical fields by majority or first
        merged_item.intent = Counter(i.intent for i in items).most_common(1)[0][0]
        merged_item.source = "+".join(sorted(set(i.source for i in items)))
        merged_item.lang = items[0].lang
        merged_item.region = items[0].region
        merged.append(merged_item)
    return merged

def normalize_market_metrics(cands: List[KeywordCandidate]) -> Dict[str, Dict[str, float]]:
    """
    Normalize volume across the batch and clamp difficulty/trend to 0..1.
    Returns a mapping term -> {volume_norm, difficulty_norm, trend_norm}.
    """
    volumes = [c.volume for c in cands]
    vol_norms = _min_max_norm(volumes) if any(v is not None for v in volumes) else [0.0] * len(cands)
    out: Dict[str, Dict[str, float]] = {}
    for idx, c in enumerate(cands):
        out[c.term] = {
            "volume_norm": vol_norms[idx],
            "difficulty_norm": _difficulty_norm(c.difficulty),
            "trend_norm": _trend_norm(c.trend),
        }
    return out

def score_keyword(c: KeywordCandidate, market: Dict[str, float], cfg: KeywordRankConfig) -> RankedKeyword:
    # Relevance components
    sem = _semantic_proxy(c)
    bm25 = _bm25_proxy(c)
    ent = _entity_presence_default(c.term, c.entity_presence)
    intent_al = _intent_alignment(c.intent, cfg.target_content_type)
    base_spec = _safe(c.specificity, 0.6)
    spec = _specificity_heuristic(c.term, base_spec, cfg)

    # Market components
    m = market.get(c.term, {"volume_norm": 0.0, "difficulty_norm": 0.5, "trend_norm": 0.5})
    vol = m["volume_norm"]
    diff = m["difficulty_norm"]
    trend = m["trend_norm"]

    # Opportunity components
    serp = _serp_openings_default(c.serp_openings)
    brand_fit = _brand_fit_default(c.brand_fit)
    comp_gap = _competitor_gap_from_strength(c.competitor_strength)

    # Region weight (optional)
    r_weight = 1.0
    if cfg.region_weights and c.region:
        r_weight = cfg.region_weights.get(c.region, 1.0)

    # Weighted sums
    wr = cfg.w_relevance
    rel = wr[0]*sem + wr[1]*bm25 + wr[2]*ent + wr[3]*intent_al + wr[4]*spec

    vm = cfg.v_market
    mkt = vm[0]*vol + vm[1]*(1.0 - diff) + vm[2]*trend

    uo = cfg.u_opportunity
    opp = uo[0]*comp_gap + uo[1]*serp + uo[2]*brand_fit

    a = cfg.alpha_beta_gamma
    final = (a[0]*rel + a[1]*mkt + a[2]*opp) * r_weight
    final = round(final, 4)

    return RankedKeyword(
        term=c.term,
        intent=c.intent,
        region=c.region,
        final_score=final,
        relevance_score=round(rel, 4),
        market_score=round(mkt, 4),
        opportunity_score=round(opp, 4),
        volume_norm=vol,
        difficulty_norm=diff,
        trend_norm=trend,
        specificity=spec,
        entity_presence=ent,
        doc_relevance=_safe(c.doc_relevance, 0.6),
        serp_openings=serp,
        competitor_gap=comp_gap,
        brand_fit=brand_fit,
        length_tokens=len(c.term.split()),
        notes=f"src={c.source}"
    )

def categorize_ranked(ranked: List[RankedKeyword], cfg: KeywordRankConfig) -> Dict[str, List[RankedKeyword]]:
    """
    Split ranked keywords into primary, secondary, and long-tail buckets.
    """
    # Base filter
    filtered = [
        r for r in ranked
        if r.relevance_score >= cfg.min_relevance
        and r.specificity >= cfg.min_specificity
        and r.volume_norm >= cfg.min_volume_norm
    ]

    # Primary
    primary = filtered[: cfg.primary_top_n]

    # Secondary (next best by final score with decent relevance)
    remaining = [r for r in ranked if r not in primary]
    secondary = []
    for r in remaining:
        if len(secondary) >= cfg.secondary_top_n:
            break
        if r.relevance_score >= cfg.min_relevance * 0.95:
            secondary.append(r)

    # Long-tail (longer phrases or highly specific)
    long_tail = [
        r for r in ranked
        if (r.length_tokens >= 3 and r.specificity >= 0.7) or r.entity_presence >= 0.8
    ][: cfg.long_tail_max_n]

    return {"primary": primary, "secondary": secondary, "long_tail": long_tail}

def rank_keywords(
    candidates: List[Dict[str, Any]],
    cfg: Optional[KeywordRankConfig] = None
) -> Dict[str, Any]:
    """
    Entry point: pass your agent's candidate keyword dicts.
    Returns: {"primary": [...], "secondary": [...], "long_tail": [...], "payload": {...}}
    """
    cfg = cfg or KeywordRankConfig()

    # Convert dicts to dataclasses
    cands = []
    for c in candidates:
        if not c or not c.get("term"):
            continue
        cands.append(KeywordCandidate(**c))

    if not cands:
        return {"primary": [], "secondary": [], "long_tail": [], "payload": {}}

    # Deduplicate and normalize
    merged = deduplicate_candidates(cands)
    market_norm = normalize_market_metrics(merged)

    # Score and sort
    scored = [score_keyword(c, market_norm, cfg) for c in merged]
    scored.sort(key=lambda x: x.final_score, reverse=True)

    # Categorize
    cats = categorize_ranked(scored, cfg)

    # Build a JSON-ready payload (metadata fields are placeholders to be populated downstream)
    payload = {
        "primary_keywords": [asdict(k) for k in cats["primary"]],
        "secondary_keywords": [asdict(k) for k in cats["secondary"]],
        "long_tail_keywords": [asdict(k) for k in cats["long_tail"]],
        "metadata": {
            "schema_type": "Product" if cfg.target_content_type.lower() == "product" else "TechArticle",
            "title": "",             # agent or subsequent step should set
            "meta_description": "",  # agent or subsequent step should set
            "h1": "",                # agent or subsequent step should set
            "h2": [],                # agent or subsequent step should set
            "url_slug": "",          # agent or subsequent step should set
            "internal_links": []     # agent or subsequent step should set
        },
        "notes": {
            "weights": {
                "relevance": cfg.w_relevance,
                "market": cfg.v_market,
                "opportunity": cfg.u_opportunity,
                "blend": cfg.alpha_beta_gamma
            },
            "thresholds": {
                "min_relevance": cfg.min_relevance,
                "min_specificity": cfg.min_specificity,
                "min_volume_norm": cfg.min_volume_norm
            }
        }
    }

    return {"primary": cats["primary"], "secondary": cats["secondary"], "long_tail": cats["long_tail"], "payload": payload}

# ----------------------------
# Optional: simple CLI entry (no hardcoded domain terms)
# ----------------------------

if __name__ == "__main__":
    # Example structure only; replace with your agent’s actual outputs
    example_candidates = [
        {"term": "example keyword one", "source": "document", "intent": "informational",
         "doc_relevance": 0.85, "entity_presence": 0.70, "specificity": 0.75,
         "volume": 1200, "difficulty": 55, "trend": 0.10, "serp_openings": 0.5,
         "competitor_strength": 0.6, "brand_fit": 0.8},

        {"term": "example keyword two", "source": "web", "intent": "technical",
         "doc_relevance": 0.80, "entity_presence": 0.65, "specificity": 0.78,
         "volume": 400, "difficulty": 45, "trend": 0.05, "serp_openings": 0.55,
         "competitor_strength": 0.5, "brand_fit": 0.85},

        {"term": "highly specific long phrase example", "source": "document+web", "intent": "informational",
         "doc_relevance": 0.90, "entity_presence": 0.85, "specificity": 0.88,
         "volume": 80, "difficulty": 35, "trend": 0.12, "serp_openings": 0.6,
         "competitor_strength": 0.4, "brand_fit": 0.9}
    ]

    cfg = KeywordRankConfig(
        target_content_type="TechArticle",
        primary_top_n=5,
        secondary_top_n=10,
        long_tail_max_n=20,
        # Set optional boosts/penalties if you want later (kept empty for general use)
        boost_patterns=(),
        generic_penalties=()
    )

    result = rank_keywords(example_candidates, cfg)
    print("Primary:")
    for k in result["primary"]:
        print(f"  {k.final_score:>6}  |  {k.term}")

    print("\nPayload keys:", list(result["payload"].keys()))
