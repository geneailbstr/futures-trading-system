"""
sentiment.py — Claude news sentiment analysis
Toggle on/off with USE_SENTIMENT in config.py
"""

import requests
from anthropic import Anthropic
from datetime import datetime
import config

client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

NEWS_FEEDS = {
    "MNQ": [
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=NQ=F&region=US&lang=en-US",
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=QQQ&region=US&lang=en-US",
    ],
    "MES": [
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=ES=F&region=US&lang=en-US",
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY&region=US&lang=en-US",
    ],
}

_sentiment_cache = {}
_cache_time      = {}
CACHE_MINUTES    = 15


def get_headlines(symbol, max_headlines=5):
    """Fetch latest headlines from Yahoo Finance RSS"""
    headlines = []
    feeds     = NEWS_FEEDS.get(symbol, NEWS_FEEDS["MES"])

    for url in feeds:
        try:
            response = requests.get(url, timeout=5)
            if response.status_code != 200:
                continue
            content = response.text
            items   = content.split("<item>")[1:]
            for item in items[:max_headlines]:
                try:
                    title = item.split("<title>")[1].split("</title>")[0]
                    title = title.replace("<![CDATA[", "").replace("]]>", "").strip()
                    if title:
                        headlines.append(title)
                except Exception:
                    continue
        except Exception as e:
            print(f"Warning: News fetch error for {symbol}: {e}")

    return headlines[:max_headlines]


def analyze_sentiment(symbol, direction):
    """
    Use Claude to score news sentiment
    Returns dict with score and trade recommendation
    """
    if not config.USE_SENTIMENT:
        return {"score": "NEUTRAL", "confidence": 0, "trade": True}

    cache_key = f"{symbol}_{direction}"
    now       = datetime.now()

    if cache_key in _sentiment_cache:
        cached_time = _cache_time.get(cache_key)
        if cached_time and (now - cached_time).seconds < CACHE_MINUTES * 60:
            return _sentiment_cache[cache_key]

    try:
        headlines = get_headlines(symbol)
        if not headlines:
            return {"score": "NEUTRAL", "confidence": 0, "trade": True}

        headlines_text = "\n".join([f"- {h}" for h in headlines])

        prompt = f"""You are analyzing market sentiment for {symbol} futures.
Direction of proposed trade: {direction}

Recent headlines:
{headlines_text}

Respond with ONLY a JSON object in this exact format:
{{
  "score": "BULLISH" or "BEARISH" or "NEUTRAL",
  "confidence": 0-100,
  "summary": "one sentence explanation",
  "trade": true or false
}}

Set trade to true if sentiment supports the {direction} direction.
Set trade to false if sentiment strongly contradicts {direction}.
Neutral sentiment should not block trades.
"""

        response = client.messages.create(
            model      = "claude-sonnet-4-6",
            max_tokens = 200,
            messages   = [{"role": "user", "content": prompt}]
        )

        import json
        text   = response.content[0].text.strip()
        text   = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)

        _sentiment_cache[cache_key] = result
        _cache_time[cache_key]      = now

        trade_str = "Trade OK" if result["trade"] else "Skip trade"
        print(f"   Sentiment ({symbol}): {result['score']} "
              f"({result['confidence']}%) — {trade_str}")
        print(f"   {result.get('summary', '')}")

        return result

    except Exception as e:
        print(f"Warning: Sentiment error: {e}")
        return {"score": "NEUTRAL", "confidence": 0, "trade": True}


def sentiment_allows_trade(symbol, direction):
    """Returns True if sentiment allows the trade to proceed"""
    if not config.USE_SENTIMENT:
        return True
    result = analyze_sentiment(symbol, direction)
    return result.get("trade", True)
