"""Normalize historical aliases and parse explicit Persian forecast fields only."""
import hashlib
import json
import math
import re
from datetime import datetime
from config import TZ_TEHRAN

HORIZONS = {'4h': 4, '24h': 24, '1w': 168}
ALIASES = {'mesghal': 'mazneh', 'mesghal_toman': 'mazneh', 'gold_18': 'geram18',
           'gold_18_toman': 'geram18', 'usd': 'dollar', 'usd_toman': 'dollar',
           'price_dollar_rl': 'dollar', 'tether': 'usdt', 'tether_toman': 'usdt', 'crypto-tether-irr': 'usdt'}
DIRECTIONS = ('صعودی', 'نزولی', 'نوسانی')


def timestamp(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_TEHRAN)
    return dt.astimezone(TZ_TEHRAN)


def number(value):
    if isinstance(value, bool):
        raise ValueError('Boolean price')
    text = str(value).translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'))
    result = float(re.sub(r'[,،\s]', '', text))
    if not math.isfinite(result) or result <= 0:
        raise ValueError('Price must be finite and positive')
    return result


def prices(values):
    result = {}
    for key, value in (values or {}).items():
        asset = ALIASES.get(key, key)
        if asset not in {'mazneh', 'geram18', 'dollar', 'usdt', 'gold_ounce', 'silver_ounce', 'sekee'}:
            continue
        try:
            result[asset] = number(value)
        except (ValueError, TypeError):
            continue
    return result


def explicit_ranges(text):
    """Read adjacent timeframe blocks, including multiline and Markdown tables.

    Do not infer direction from news or other prose. Missing fields stay missing.
    """
    text = text.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789')).replace('*', '').replace('\u200c', ' ')
    marker = r'(?<!\d)(24\s*ساعت\S*|4\s*ساعت\S*|1\s*هفته\S*|یک\s*هفته\S*|24h|4h|1w)'
    matches = list(re.finditer(marker, text, re.I))
    result = {}
    for i, match in enumerate(matches):
        label = match.group(0)
        key = '24h' if label.startswith('24') else '4h' if label.startswith('4') else '1w'
        stop = matches[i+1].start() if i+1 < len(matches) else len(text)
        block = text[match.end():stop][:350]
        direction = next((d for d in DIRECTIONS if d in block), None)
        bounds = re.search(r'([\d][\d,،]*)\s*(?:تا|—|–|-)\s*([\d][\d,،]*)', block)
        if direction and bounds:
            try:
                low, high = map(number, bounds.groups())
            except ValueError:
                continue
            if low <= high:
                result[key] = {'min': low, 'max': high, 'direction': direction}
    return result


def normalize_forecast(raw):
    raw = dict(raw)
    text = str(raw.get('forecast_text', ''))
    snap = raw.get('snapshot') or {'prices': raw.get('snapshot_prices', {})}
    snap = dict(snap)
    snap['prices'] = prices(snap.get('prices', {}))
    ts = timestamp(raw['timestamp']).isoformat()
    # Own structured models must not invent extra horizons by parsing prose/news.
    ranges = {} if raw.get('model_version') in ('hermes-analyst-v1', 'log-return-baseline-v1') else explicit_ranges(text)
    supplied = raw.get('predicted_ranges') or {}
    for old, new in [('predicted_4h', '4h'), ('predicted_24h', '24h')]:
        if raw.get(old):
            supplied = dict(supplied, **{new: raw[old]})
    for key, bounds in supplied.items():
        key = '1w' if key in ('7d', '1week') else key
        if key in HORIZONS and isinstance(bounds, dict):
            try:
                low, high = number(bounds['min']), number(bounds['max'])
                direction = bounds.get('direction') or raw.get('predicted_direction')
                if low <= high and direction in DIRECTIONS:
                    ranges[key] = dict(bounds, min=low, max=high, direction=direction)
            except (ValueError, KeyError, TypeError):
                continue
    # Identify gold18 vs mazneh from explicit asset or clearly distinct scale.
    # Never guess between dollar and USDT from their similar numeric values.
    asset = raw.get('asset')
    if asset not in snap['prices']:
        asset = None
        centers = [(b['min'] + b['max']) / 2 for b in ranges.values()]
        if centers:
            center = sum(centers) / len(centers)
            candidates = [a for a in ('mazneh', 'geram18') if a in snap['prices'] and .7 < center / snap['prices'][a] < 1.3]
            if len(candidates) == 1:
                asset = candidates[0]
    for bounds in ranges.values():
        bounds['asset'] = bounds.get('asset') or asset
    signal = raw.get('buy_signal')
    if signal not in ('BUY', 'STRONG_BUY', 'WAIT', 'STRONG_WAIT'):
        match = re.search(r'\b(STRONG[_ ]BUY|STRONG[_ ]WAIT|BUY|WAIT)\b', text)
        signal = match.group(1).replace(' ', '_') if match else None
    identity = json.dumps([ts, ' '.join(text.split()), snap['prices']], sort_keys=True, ensure_ascii=False)
    return {'id': raw.get('id') or hashlib.sha256(identity.encode()).hexdigest()[:32],
            'timestamp': ts, 'forecast_text': text, 'snapshot': snap, 'asset': asset,
            'predicted_ranges': ranges, 'predicted_direction': ranges.get('24h', {}).get('direction'),
            'buy_signal': signal, 'confidence': raw.get('confidence'), 'reasoning': raw.get('reasoning', ''),
            'model_version': raw.get('model_version', 'legacy-unverified'),
            'analyst': raw.get('analyst'), 'cycle_id': raw.get('cycle_id'),
            'bundle_sha256': raw.get('bundle_sha256'),
            'evaluation': None, 'evaluations': {}, 'legacy_evaluation': raw.get('legacy_evaluation', raw.get('evaluation'))}
