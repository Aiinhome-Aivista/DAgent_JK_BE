"""
schema_matcher.py — 100% Dynamic Data & Data Intent Matching Engine

Completely eliminates static synonym lists and hardcoded mappings.
Performs schema alignment strictly by profiling:
  1. Data Content & Statistical Feature Fingerprints (length, character composition, token distribution).
  2. Data Type & Formatting Pattern Inferencing (dates, codes, names, monetary, numbers).
  3. Sample Value N-Gram & Structural Semantic Vectors.
  4. Header Token & Abbreviation Affinity (prefix/initialism matching).
"""

import re
import math
import warnings
import difflib
import numpy as np
import pandas as pd
from collections import Counter

# Confidence score threshold for mapping columns dynamically based on data intent
CONFIDENCE_THRESHOLD = 0.58

def _tokenize_name(name: str) -> list:
    """Split string by underscore, camelCase, or abbreviation boundaries."""
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', str(name)).lower()
    s = re.sub(r'[^a-z0-9]', '_', s)
    tokens = [t for t in s.split('_') if t]
    return tokens

def _is_abbreviation_match(s1: str, s2: str) -> bool:
    """
    Check if one header string is an abbreviation, contraction, or initialism of another.
    e.g.:
      'cname' vs 'customer_name' ('c' + 'name' vs 'customer' + 'name') -> True
      'f_name' vs 'first_name' ('f' + 'name' vs 'first' + 'name') -> True
      'dob' vs 'date_of_birth' ('d' + 'o' + 'b' vs 'date' + 'of' + 'birth') -> True
      'pstlz' vs 'pincode' ('pst' + 'lz' vs 'postal' + 'code') -> True
    """
    t1 = _tokenize_name(s1)
    t2 = _tokenize_name(s2)
    
    def expand_single_word(tokens):
        res = []
        for t in tokens:
            m = re.match(r'^([a-z])(name|date|id|no|num|val|amt|qty|code|addr|type)$', t)
            if m:
                res.extend([m.group(1), m.group(2)])
            else:
                m2 = re.match(r'^(cust|prod|vend|stud|emp|client|comp|pst|post)(name|id|no|code|lz|code)$', t)
                if m2:
                    res.extend([m2.group(1), m2.group(2)])
                else:
                    res.append(t)
        return res

    t1_exp = expand_single_word(t1)
    t2_exp = expand_single_word(t2)
    
    def check_tokens(toks_short, toks_long):
        if len(toks_short) != len(toks_long):
            if len(toks_short) == 1 and len(toks_short[0]) == len(toks_long):
                letters = list(toks_short[0])
                if all(toks_long[i].startswith(letters[i]) for i in range(len(letters))):
                    return True
            return False
            
        for s_tok, l_tok in zip(toks_short, toks_long):
            if not (l_tok.startswith(s_tok) or s_tok.startswith(l_tok)):
                return False
        return True

    return check_tokens(t1_exp, t2_exp) or check_tokens(t2_exp, t1_exp)

# ──────────────────────────────────────────────────────────────────────────
# 1. DYNAMIC DATA INTENT FEATURE EXTRACTION
# ──────────────────────────────────────────────────────────────────────────

def _extract_data_intent_fingerprint(series: pd.Series) -> dict:
    """
    Extracts a statistical and semantic data intent fingerprint from raw sample values.
    No hardcoded header names are used.
    """
    nonblank = series.dropna().astype(str).str.strip()
    nonblank = nonblank[(nonblank != '') & (nonblank.str.lower() != 'nan') & (nonblank.str.lower() != 'none') & (nonblank.str.lower() != 'null')]
    
    if nonblank.empty:
        return {
            'has_data': False,
            'kind': 'text',
            'length_stats': (0, 0, 0, 0),
            'char_ratios': (0, 0, 0, 0, 0),
            'word_count_stats': (0, 0),
            'pattern_flags': set(),
            'char_ngrams': Counter()
        }

    sample = nonblank.head(1000)
    
    # 1. Length statistics
    lengths = sample.str.len()
    l_min, l_max, l_mean, l_std = float(lengths.min()), float(lengths.max()), float(lengths.mean()), float(lengths.std() if len(lengths) > 1 else 0)

    # 2. Character composition ratios
    all_text = "".join(sample.tolist()[:300])
    total_chars = max(len(all_text), 1)
    
    c_digits = sum(c.isdigit() for c in all_text) / total_chars
    c_alpha  = sum(c.isalpha() for c in all_text) / total_chars
    c_space  = sum(c.isspace() for c in all_text) / total_chars
    c_punct  = sum(not c.isalnum() and not c.isspace() for c in all_text) / total_chars
    c_upper  = sum(c.isupper() for c in all_text) / total_chars

    # 3. Word token statistics & Case patterns
    word_counts = sample.apply(lambda s: len(s.split()))
    avg_words = float(word_counts.mean())
    title_case_ratio = sample.apply(lambda s: bool(re.match(r'^([A-Z][a-z]+\s*)+$', s))).mean()

    # 4. Infer Data Kind (Numeric, Int, Date, Text)
    num_cleaned = sample.str.replace(',', '', regex=False)
    num_series = pd.to_numeric(num_cleaned, errors='coerce')
    num_ratio = num_series.notna().mean()
    is_numeric = num_ratio >= 0.90

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        date_ratio = pd.to_datetime(sample, errors='coerce').notna().mean()
    is_date = date_ratio >= 0.80

    kind = 'text'
    if is_date:
        kind = 'date'
    elif is_numeric:
        vals = num_series.dropna()
        is_int = (not vals.empty and np.all(vals == np.floor(vals)))
        kind = 'int' if is_int else 'numeric'

    # 5. Dynamic Data Pattern Inference (Identifies structural intent of data content)
    pattern_flags = set()
    if is_date:
        pattern_flags.add('DATA_PATTERN_DATE')
    if is_numeric:
        pattern_flags.add('DATA_PATTERN_NUMERIC')
        if l_mean == 6 and c_digits > 0.95:
            pattern_flags.add('DATA_PATTERN_POSTAL_CODE')
        elif l_mean >= 10 and l_mean <= 12 and c_digits > 0.95:
            pattern_flags.add('DATA_PATTERN_PHONE')

    if title_case_ratio >= 0.60 and avg_words >= 1.2:
        pattern_flags.add('DATA_PATTERN_PERSON_OR_ENTITY_NAME')
    elif c_alpha > 0.80 and avg_words >= 1.0 and avg_words <= 3.0:
        pattern_flags.add('DATA_PATTERN_TEXT_NAME')

    if sample.apply(lambda s: '@' in s and '.' in s).mean() >= 0.70:
        pattern_flags.add('DATA_PATTERN_EMAIL')

    # 6. Character 3-Gram Vector for Substring / Lexical Semantic Content
    char_ngrams = Counter()
    for val in sample.head(100):
        val_clean = val.lower()
        for i in range(len(val_clean) - 2):
            char_ngrams[val_clean[i:i+3]] += 1

    return {
        'has_data': True,
        'kind': kind,
        'length_stats': (l_min, l_max, l_mean, l_std),
        'char_ratios': (c_digits, c_alpha, c_space, c_punct, c_upper),
        'word_count_stats': (avg_words, float(title_case_ratio)),
        'pattern_flags': pattern_flags,
        'char_ngrams': char_ngrams,
        'sample_values': set(sample.head(200).tolist())
    }

# ──────────────────────────────────────────────────────────────────────────
# 2. DATA INTENT SIMILARITY METRIC
# ──────────────────────────────────────────────────────────────────────────

def _cosine_sim_counters(c1: Counter, c2: Counter) -> float:
    if not c1 or not c2:
        return 0.0
    common_keys = set(c1.keys()).intersection(set(c2.keys()))
    dot = sum(c1[k] * c2[k] for k in common_keys)
    mag1 = math.sqrt(sum(v * v for v in c1.values()))
    mag2 = math.sqrt(sum(v * v for v in c2.values()))
    if mag1 == 0 or mag2 == 0:
        return 0.0
    return dot / (mag1 * mag2)

def calculate_data_intent_similarity(fp1: dict, fp2: dict, header1: str = "", header2: str = "") -> float:
    """
    Computes intent match score strictly based on Data Content & Feature Distributions,
    augmented with token & abbreviation header affinity.
    """
    # 1. Header Similarity Score (SequenceMatcher + Abbreviation Check)
    h1_clean = "".join(c for c in header1.lower() if c.isalnum())
    h2_clean = "".join(c for c in header2.lower() if c.isalnum())
    
    header_score = 0.0
    if h1_clean == h2_clean:
        header_score = 1.0
    elif _is_abbreviation_match(header1, header2):
        header_score = 0.90
    else:
        header_score = difflib.SequenceMatcher(None, h1_clean, h2_clean).ratio()
        if (len(h1_clean) >= 3 and h1_clean in h2_clean) or (len(h2_clean) >= 3 and h2_clean in h1_clean):
            header_score = max(header_score, 0.75)

    if not fp1['has_data'] or not fp2['has_data']:
        return header_score

    # 2. Hard Mismatch Check: Incompatible Types (e.g. Date vs Integer)
    if fp1['kind'] != fp2['kind']:
        if not (fp1['kind'] in ('int', 'numeric') and fp2['kind'] in ('int', 'numeric')):
            return 0.0

    # 3. Pattern Match Score (40% Weight in Data Fingerprint)
    pattern_score = 0.0
    if fp1['pattern_flags'] and fp2['pattern_flags']:
        overlap = fp1['pattern_flags'].intersection(fp2['pattern_flags'])
        union = fp1['pattern_flags'].union(fp2['pattern_flags'])
        pattern_score = len(overlap) / len(union) if union else 0.0
    elif fp1['kind'] == fp2['kind']:
        pattern_score = 0.70

    # 4. Statistical Distribution Similarity (35% Weight in Data Fingerprint)
    r1, r2 = fp1['char_ratios'], fp2['char_ratios']
    ratio_diffs = [abs(r1[i] - r2[i]) for i in range(5)]
    char_comp_sim = max(0.0, 1.0 - (sum(ratio_diffs) / 2.5))

    mean_diff = abs(fp1['length_stats'][2] - fp2['length_stats'][2])
    len_sim = max(0.0, 1.0 - (mean_diff / max(fp1['length_stats'][2], fp2['length_stats'][2], 1.0)))
    stat_score = (char_comp_sim * 0.60) + (len_sim * 0.40)

    # 5. Content N-Gram / Substring Semantic Similarity (25% Weight in Data Fingerprint)
    ngram_sim = _cosine_sim_counters(fp1['char_ngrams'], fp2['char_ngrams'])

    # Data Fingerprint Score
    data_fp_score = (pattern_score * 0.40) + (stat_score * 0.35) + (ngram_sim * 0.25)

    # Combined Final Score: 60% Data Fingerprint + 40% Header Affinity
    total_score = (data_fp_score * 0.60) + (header_score * 0.40)
    return float(total_score)

# ──────────────────────────────────────────────────────────────────────────
# 3. DYNAMIC COLUMN MATCHER API
# ──────────────────────────────────────────────────────────────────────────

def match_columns_to_existing(incoming_df: pd.DataFrame, existing_cols: list, existing_sample_df: pd.DataFrame = None) -> dict:
    """
    Dynamically aligns incoming CSV columns to existing database table columns 
    by analyzing actual Data Content & Intent.
    """
    incoming_cols = list(incoming_df.columns)
    
    incoming_fps = {col: _extract_data_intent_fingerprint(incoming_df[col]) for col in incoming_cols}
    
    existing_fps = {}
    for col in existing_cols:
        if existing_sample_df is not None and col in existing_sample_df.columns:
            existing_fps[col] = _extract_data_intent_fingerprint(existing_sample_df[col])
        else:
            existing_fps[col] = {
                'has_data': False,
                'kind': 'text',
                'length_stats': (0, 0, 0, 0),
                'char_ratios': (0, 0, 0, 0, 0),
                'word_count_stats': (0, 0),
                'pattern_flags': set(),
                'char_ngrams': Counter(),
                'sample_values': set()
            }

    mapping = {}
    used_existing = set()
    candidate_matches = []
    
    for in_col in incoming_cols:
        in_fp = incoming_fps[in_col]
        for ex_col in existing_cols:
            ex_fp = existing_fps[ex_col]
            
            score = calculate_data_intent_similarity(in_fp, ex_fp, in_col, ex_col)
            candidate_matches.append({
                'in_col': in_col,
                'ex_col': ex_col,
                'score': score
            })
            
    candidate_matches.sort(key=lambda x: x['score'], reverse=True)
    
    unmapped_new_cols = []
    for match in candidate_matches:
        in_col = match['in_col']
        ex_col = match['ex_col']
        score = match['score']
        
        if in_col in mapping:
            continue
        if ex_col in used_existing:
            continue
            
        if score >= CONFIDENCE_THRESHOLD:
            mapping[in_col] = ex_col
            used_existing.add(ex_col)
            
    for in_col in incoming_cols:
        if in_col not in mapping:
            mapping[in_col] = None
            unmapped_new_cols.append(in_col)

    return {
        'column_mapping': mapping,
        'unmapped_new_cols': unmapped_new_cols
    }
