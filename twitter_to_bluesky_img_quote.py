#!/usr/bin/env python3
import json
import os
from typing import List, Dict

import random
import time
import re

import requests
import feedparser
from bs4 import BeautifulSoup
from atproto import Client

# Add random jitter (0–1800 seconds)
jitter_seconds = random.randint(0, 1800)
print(f"Sleeping for {jitter_seconds} seconds before running...")
time.sleep(jitter_seconds)

# ========== CONFIGURATION ==========

GIST_ID = os.environ.get("GIST_ID")
GIST_TOKEN = os.environ.get("GIST_TOKEN")
STATE_FILENAME = "posted_tweets.json"

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
    "jackiewaspushed",
    "LuisFernandoPHL"
]

# How many recent tweets per user to consider each run
TWEETS_PER_USER = 10

# Bluesky credentials
BSKY_HANDLE = os.environ.get("BSKY_HANDLE")
BSKY_APP_PASSWORD = os.environ.get("BSKY_APP_PASSWORD")

# Max characters per Bluesky post (Bluesky default is 300)
MAX_BSKY_CHARS = 300

# Base URL for Nitter-style RSS mirror
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
    """
    Load state from a GitHub Gist.
    The gist must contain a file named STATE_FILENAME with JSON like:
    { "tweet_ids": ["123", "456"] }
    """
    if not GIST_ID or not GIST_TOKEN:
        print("WARNING: GIST_ID or GIST_TOKEN not set, using empty in-memory state.")
        return {"tweet_ids": set()}

    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {
        "Authorization": f"token {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"ERROR: Failed to fetch gist state: {e}")
        # fall back to empty state so script still runs
        return {"tweet_ids": set()}

    data = resp.json()
    files = data.get("files", {})
    file_obj = files.get(STATE_FILENAME)

    if not file_obj or "content" not in file_obj:
        print(f"WARNING: {STATE_FILENAME} not found in gist, starting fresh.")
        return {"tweet_ids": set()}

    try:
        content = file_obj["content"]
        state_json = json.loads(content)
        tweet_ids = set(state_json.get("tweet_ids", []))
        print(f"Loaded {len(tweet_ids)} previously posted tweet IDs from gist.")
        return {"tweet_ids": tweet_ids}
    except Exception as e:
        print(f"ERROR: Failed to parse gist content, starting fresh: {e}")
        return {"tweet_ids": set()}


def save_state(state: Dict) -> None:
    """
    Save state back to GitHub Gist.
    Writes JSON into STATE_FILENAME in the gist.
    """
    if not GIST_ID or not GIST_TOKEN:
        print("WARNING: GIST_ID or GIST_TOKEN not set, state will not be persisted.")
        return

    tweet_ids = sorted(list(state.get("tweet_ids", [])))
    content_str = json.dumps({"tweet_ids": tweet_ids}, indent=2)

    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {
        "Authorization": f"token {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "files": {
            STATE_FILENAME: {
                "content": content_str
            }
        }
    }

    try:
        resp = requests.patch(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"Saved {len(tweet_ids)} tweet IDs to gist.")
    except requests.RequestException as e:
        print(f"ERROR: Failed to save gist state: {e}")


def looks_like_retweet(title: str) -> bool:
    """
    Heuristic to detect retweets from the RSS title.
    Common patterns: "RT @user:" or starting with "RT ".
    """
    t = title.strip()
    if t.lower().startswith("rt @") or t.lower().startswith("rt "):
        return True
    return False


def extract_media_urls_from_entry(entry) -> List[str]:
    """
    Extract image URLs from the RSS entry HTML.
    For Nitter, images look like:
      <img src="https://nitter.net/pic/media%2FGkfTMhmXMAAc85u.jpg" ...>
    """
    media_urls: List[str] = []

    html = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
    for match in re.findall(r'<img[^>]+src="([^"]+)"', html):
        media_urls.append(match)

    return media_urls


def parse_entry_text_and_quote(entry) -> Dict[str, str | None]:
    """
    Parse the entry HTML to extract:
      - main_text: the tweet author's own text
      - quote_author: handle of quoted user (if any)
      - quote_text: text from the quoted tweet (if any)
    """
    title = getattr(entry, "title", "") or ""
    html = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""

    if not html:
        # Fallback: just use the title as main text
        return {
            "main_text": title,
            "quote_author": None,
            "quote_text": None,
        }

    soup = BeautifulSoup(html, "html.parser")

    # Find quote block, if present
    quote_block = soup.find("blockquote")

    # --- Main text: all <p> tags NOT inside a blockquote ---
    main_paras: List[str] = []
    for p in soup.find_all("p"):
        if p.find_parent("blockquote") is None:
            txt = p.get_text(" ", strip=True)
            if txt:
                main_paras.append(txt)

    main_text = "\n".join(main_paras).strip()
    if not main_text:
        # Fallback to title if we couldn't get anything
        main_text = title

    # --- Quoted tweet info, if any ---
    quote_author: str | None = None
    quote_text: str | None = None

    if quote_block:
        # Author often in <b>PopPulse (@PoppPulse)</b>
        b = quote_block.find("b")
        if b:
            b_text = b.get_text(" ", strip=True)
            m = re.search(r'@([A-Za-z0-9_]+)', b_text)
            if m:
                quote_author = m.group(1)  # without '@'
            else:
                quote_author = b_text

        # Text: gather all <p> inside the blockquote
        quote_paras: List[str] = [
            p.get_text(" ", strip=True) for p in quote_block.find_all("p")
        ]
        qt = " ".join(quote_paras).strip()
        if qt:
            quote_text = qt

    return {
        "main_text": main_text,
        "quote_author": quote_author,
        "quote_text": quote_text,
    }


def get_recent_tweets_rss(username: str, limit: int) -> List[Dict]:
    """
    Fetch last `limit` tweets for a given username via an RSS mirror.
    Returns list of dicts with id, content, url, media_urls, quote_* fields.
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
        title = getattr(entry, "title", "") or ""

        # skip retweets based on title
        if looks_like_retweet(title):
            print(f"  Skipping retweet: {title[:60]!r}")
            continue

        parsed = parse_entry_text_and_quote(entry)
        content = parsed["main_text"]
        quote_author = parsed["quote_author"]
        quote_text = parsed["quote_text"]

        if not content:
            # nothing to post, skip
            continue

        tweet_id = getattr(entry, "id", None) or entry.link
        url = entry.link

        media_urls = extract_media_urls_from_entry(entry)

        tweets.append(
            {
                "id": str(tweet_id),
                "content": content,
                "url": url,
                "username": username,
                "media_urls": media_urls,
                "quote_author": quote_author,
                "quote_text": quote_text,
            }
        )

        if len(tweets) >= limit:
            break

    print(f"  Using {len(tweets)} tweets for @{username} after filtering.")
    return tweets


def format_bsky_post(tweet: Dict) -> str:
    """
    Format text for the Bluesky post.
    Includes quote info if present.
    Truncates to MAX_BSKY_CHARS.
    """
    main = tweet["content"]
    quote_author = tweet.get("quote_author")
    quote_text = tweet.get("quote_text")

    if quote_author and quote_text:
        base_text = f"""@{tweet['username']}:
    
{main}

——
Quoted @{quote_author}:
{quote_text}"""
    else:
        base_text = f"""@{tweet['username']}:
    
{main}"""

    if len(base_text) <= MAX_BSKY_CHARS:
        return base_text

    # Truncation: simplify to trimming the combined block
    return base_text[:MAX_BSKY_CHARS].rstrip() + "…"


def post_to_bluesky(client: Client, tweet: Dict, text: str) -> None:
    """
    Post a single tweet to Bluesky using atproto Client.
    If media URLs are present, download up to 4 images and use send_images.
    Otherwise, fall back to a text-only post.
    """
    media_urls: List[str] = tweet.get("media_urls") or []

    # No media → just text
    if not media_urls:
        client.send_post(text)
        return

    images: List[bytes] = []
    image_alts: List[str] = []

    # Limit to 4 images (Bluesky max)
    for url in media_urls[:4]:
        try:
            print(f"    Downloading image: {url}")
            r = requests.get(url, headers=HTTP_HEADERS, timeout=15)
            r.raise_for_status()
            images.append(r.content)
            image_alts.append(f"Image from tweet by @{tweet['username']}")
        except Exception as e:
            print(f"    ERROR downloading image {url}: {e}")

    if not images:
        # If we failed to download any images, fall back to text
        print("    No images successfully downloaded; posting text-only.")
        client.send_post(text)
        return

    # High-level helper from atproto to send images
    client.send_images(text=text, images=images, image_alts=image_alts)


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
                post_to_bluesky(client, tweet, text)
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