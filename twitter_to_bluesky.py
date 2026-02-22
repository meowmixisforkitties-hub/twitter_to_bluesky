#!/usr/bin/env python3
import json
import os
from pathlib import Path
from typing import List, Dict

import requests
import feedparser
from atproto import Client

import random
import time

# Add random jitter (0–1800 seconds)
jitter_seconds = random.randint(0, 1800)
print(f"Sleeping for {jitter_seconds} seconds before running...")
time.sleep(jitter_seconds)

# ========== CONFIGURATION ==========

# Twitter usernames (without the @)
TWITTER_USERNAMES = [
    "PapaBowflex",
    "alli_mcbeal",
    "nick_cas90",
    "RedDlicious",
    "ThisIsMyCourt",
    "pugslovepizza",
    "keenanwho",
    "ericmichael82",
    "rhinojawn",
    "yugyawdaorb",
    "jackiewaspushed"
]

# How many recent tweets per user to consider each run
TWEETS_PER_USER = 10

# Bluesky credentials
# You can hardcode or pull from environment variables
BSKY_HANDLE = os.environ.get("BSKY_HANDLE")
BSKY_APP_PASSWORD = os.environ.get("BSKY_APP_PASSWORD")

# Path to JSON file that remembers which tweets we've already posted
STATE_FILE = Path(__file__).with_name("posted_tweets.json")

# Max characters per Bluesky post (Bluesky default is 300)
MAX_BSKY_CHARS = 300

# Base URL for Nitter-style RSS mirror
# If this instance is flaky, change the domain.
NITTER_RSS_TEMPLATE = "https://nitter.net/{username}/rss"

# Some Nitter instances are picky about User-Agent
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0 Safari/537.36"
    )
}


# ========== HELPER FUNCTIONS ==========


def load_state() -> Dict:
    """Load state from STATE_FILE; returns dict with 'tweet_ids' set."""
    if STATE_FILE.exists():
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    tweet_ids = set(data.get("tweet_ids", []))
    return {"tweet_ids": tweet_ids}


def save_state(state: Dict) -> None:
    """Save state back to STATE_FILE."""
    data = {
        "tweet_ids": sorted(list(state["tweet_ids"])),
    }
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def looks_like_retweet(title: str) -> bool:
    """
    Heuristic to detect retweets from the RSS title.
    Common patterns: "RT @user:" or starting with "RT ".
    """
    t = title.strip()
    if t.lower().startswith("rt @") or t.lower().startswith("rt "):
        return True
    # you can add more patterns if you notice different formats
    return False


def get_recent_tweets_rss(username: str, limit: int) -> List[Dict]:
    """
    Fetch last `limit` tweets for a given username via an RSS mirror.
    Returns list of dicts with id, content, url.
    Skips retweets.
    """
    rss_url = NITTER_RSS_TEMPLATE.format(username=username)
    print(f"Fetching RSS for @{username} from {rss_url}")

    try:
        resp = requests.get(rss_url, headers=HTTP_HEADERS, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"ERROR: Failed to fetch RSS for @{username}: {e}")
        return []

    feed = feedparser.parse(resp.text)
    entries = feed.entries
    print(f"  Found {len(entries)} items in RSS feed for @{username}")

    tweets: List[Dict] = []
    for entry in entries:
        # Nitter RSS usually has tweet text in title or summary
        title = getattr(entry, "title", "") or ""
        summary = getattr(entry, "summary", "") or ""

        # skip retweets
        if looks_like_retweet(title):
            print(f"  Skipping retweet: {title[:60]!r}")
            continue

        content = title or summary
        if not content:
            # nothing to post, skip
            continue

        tweet_id = getattr(entry, "id", None) or entry.link
        url = entry.link

        tweets.append(
            {
                "id": str(tweet_id),
                "content": content,
                "url": url,
                "username": username,
            }
        )

        if len(tweets) >= limit:
            break

    print(f"  Using {len(tweets)} tweets for @{username} after filtering.")
    return tweets


def format_bsky_post(tweet: Dict) -> str:
    """
    Format text for the Bluesky post.
    Truncates to MAX_BSKY_CHARS.
    """
    base_text = f"""@{tweet['username']}:
    
{tweet['content']}"""

    if len(base_text) <= MAX_BSKY_CHARS:
        return base_text

    reserved = 80  # space for attribution + URL
    truncated_body = tweet["content"][: MAX_BSKY_CHARS - reserved].rstrip()
    text = f"""@{tweet['username']}:
    
{truncated_body}…"""
    return text[:MAX_BSKY_CHARS]


def post_to_bluesky(client: Client, text: str) -> None:
    """Post a single text post to Bluesky using atproto Client."""
    client.send_post(text)


# ========== MAIN LOGIC ==========


def main():
    if not BSKY_HANDLE or not BSKY_APP_PASSWORD:
        raise RuntimeError(
            "Bluesky handle or app password not set. "
            "Set BSKY_HANDLE and BSKY_APP_PASSWORD env vars or edit the script."
        )

    state = load_state()
    seen_ids = state["tweet_ids"]

    print(f"Loaded {len(seen_ids)} previously posted tweet IDs.")

    # Login to Bluesky
    client = Client()
    client.login(BSKY_HANDLE, BSKY_APP_PASSWORD)
    print(f"Logged into Bluesky as {BSKY_HANDLE}")

    new_posts_count = 0

    for username in TWITTER_USERNAMES:
        print(f"\nProcessing @{username}...")
        tweets = get_recent_tweets_rss(username, TWEETS_PER_USER)

        # Process in reverse chronological so oldest of the batch posts first
        for tweet in reversed(tweets):
            tweet_id = tweet["id"]

            if tweet_id in seen_ids:
                continue

            text = format_bsky_post(tweet)
            try:
                post_to_bluesky(client, text)
                print(f"  Posted tweet {tweet_id} from @{username} to Bluesky.")
                seen_ids.add(tweet_id)
                new_posts_count += 1
            except Exception as e:
                print(f"  ERROR posting tweet {tweet_id} from @{username}: {e}")

    state["tweet_ids"] = seen_ids
    save_state(state)
    print(f"\nDone. Posted {new_posts_count} new tweets this run.")


if __name__ == "__main__":
    main()
