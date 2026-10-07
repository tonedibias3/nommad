#!/usr/bin/env python3
"""
Nommad Reddit Scraper
Pulls restaurant mentions from Augusta-area Reddit subs,
scores them by sentiment + frequency, outputs local-intel.json
"""

import json
import re
import time
import urllib.request
import urllib.parse
from collections import defaultdict
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
SUBREDDITS = [
    'Augusta',
    'AugustaGA',
]

SEARCH_QUERIES = [
    'restaurant',
    'food',
    'eat',
    'pizza',
    'bbq',
    'brunch',
    'bar',
    'dinner',
    'lunch',
    'best places',
    'hidden gem',
    'where to eat',
    'recommendations',
]

# Positive signal words
POS_WORDS = [
    'amazing', 'great', 'excellent', 'best', 'love', 'loved', 'fantastic',
    'delicious', 'incredible', 'outstanding', 'perfect', 'must try', 'must-try',
    'gem', 'hidden gem', 'underrated', 'recommend', 'go-to', 'goto', 'favorite',
    'favourite', 'good', 'solid', 'legit', 'fire', 'hits', 'worth it', 'top',
    'fresh', 'quality', 'authentic', 'real', 'homemade', 'local',
]

# Negative signal words
NEG_WORDS = [
    'terrible', 'awful', 'bad', 'worst', 'horrible', 'disgusting', 'avoid',
    'never again', 'overrated', 'disappointing', 'mediocre', 'bland',
    'rude', 'slow', 'wrong order', 'cold food', 'dirty', 'closed',
]

# Known chains to skip scoring (we already filter these in the app)
SKIP_NAMES = [
    'mcdonald', 'burger king', 'wendy', 'chick-fil-a', 'taco bell', 'subway',
    'domino', 'pizza hut', 'papa john', 'popeyes', 'kfc', 'bojangles',
    'sonic', 'waffle house', 'cracker barrel', 'panera', 'starbucks',
    'chipotle', 'five guys', 'applebee', "chili's", 'olive garden',
    'texas roadhouse', 'outback', 'red lobster', 'cheesecake factory',
    'carolina ale house', 'world of beer', "cheddar's",
]

# Augusta neighborhoods / context words (helps confirm it's local)
LOCAL_CONTEXT = [
    'augusta', 'csra', 'evans', 'martinez', 'grovetown', 'aiken',
    'north augusta', 'downtown', 'broad street', 'whiskey bar road',
]

HEADERS = {
    'User-Agent': 'Nommad/1.0 (restaurant discovery app; contact: nommad@example.com)',
    'Accept': 'application/json',
}

def fetch_reddit(url, retries=3):
    """Fetch JSON from Reddit's API with retries."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            print(f"  Attempt {attempt+1} failed for {url}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None

def search_subreddit(subreddit, query, limit=25):
    """Search a subreddit for posts matching query."""
    q = urllib.parse.quote(query)
    url = f"https://www.reddit.com/r/{subreddit}/search.json?q={q}&restrict_sr=1&sort=relevance&limit={limit}&t=year"
    data = fetch_reddit(url)
    if not data:
        return []
    posts = []
    try:
        for child in data['data']['children']:
            p = child['data']
            posts.append({
                'title': p.get('title', ''),
                'selftext': p.get('selftext', ''),
                'score': p.get('score', 0),
                'num_comments': p.get('num_comments', 0),
                'url': p.get('url', ''),
                'id': p.get('id', ''),
            })
    except (KeyError, TypeError):
        pass
    return posts

def get_post_comments(post_id, subreddit, limit=50):
    """Fetch top comments for a post."""
    url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json?limit={limit}&depth=2"
    data = fetch_reddit(url)
    if not data or len(data) < 2:
        return []
    comments = []
    def extract(node):
        if not node:
            return
        if isinstance(node, dict):
            kind = node.get('kind')
            if kind == 't1':  # comment
                body = node['data'].get('body', '')
                score = node['data'].get('score', 0)
                if score > 0 and len(body) > 10:
                    comments.append({'body': body, 'score': score})
            children = node.get('data', {}).get('children', [])
            for c in children:
                extract(c)
        elif isinstance(node, list):
            for item in node:
                extract(item)
    try:
        extract(data[1])
    except Exception:
        pass
    return comments

def extract_restaurant_names(text):
    """
    Heuristic: find capitalized multi-word phrases that look like restaurant names.
    Filters out common false positives.
    """
    # Common false positive starts
    SKIP_STARTS = {
        'I', 'We', 'They', 'He', 'She', 'It', 'The', 'A', 'An',
        'My', 'Our', 'Your', 'His', 'Her', 'Their', 'This', 'That',
        'These', 'Those', 'There', 'Here', 'augusta', 'Augusta',
        'Reddit', 'Google', 'Yelp', 'DoorDash', 'GrubHub',
        'January','February','March','April','May','June','July',
        'August','September','October','November','December',
        'Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday',
    }
    # Pattern: 1-4 capitalized words (restaurant name pattern)
    pattern = r'\b([A-Z][a-zA-Z\'&]+(?:\s+[A-Z][a-zA-Z\'&]+){0,3})\b'
    matches = re.findall(pattern, text)
    names = []
    for m in matches:
        words = m.split()
        if words[0] in SKIP_STARTS:
            continue
        if len(m) < 3 or len(m) > 50:
            continue
        # Skip if it's all caps (acronym) and short
        if m.isupper() and len(m) < 5:
            continue
        names.append(m)
    return names

def score_context(text, restaurant_name):
    """
    Score the sentiment around a restaurant mention.
    Returns a float: positive = good signal, negative = bad signal.
    """
    text_lower = text.lower()
    name_lower = restaurant_name.lower()

    # Find the mention position
    idx = text_lower.find(name_lower)
    if idx == -1:
        return 0

    # Look at a window of text around the mention
    window_start = max(0, idx - 120)
    window_end = min(len(text), idx + len(restaurant_name) + 120)
    window = text_lower[window_start:window_end]

    score = 0
    for word in POS_WORDS:
        if word in window:
            score += 1
    for word in NEG_WORDS:
        if word in window:
            score -= 2

    # Bonus: local context words nearby
    for ctx in LOCAL_CONTEXT:
        if ctx in window:
            score += 0.5
            break

    return score

def is_skippable(name):
    name_lower = name.lower()
    for skip in SKIP_NAMES:
        if skip in name_lower:
            return True
    return False

def normalize_name(name):
    """Light normalization for deduplication."""
    return re.sub(r'\s+', ' ', name.strip().lower())

def run_scraper():
    print("=" * 60)
    print("Nommad Reddit Scraper — Augusta, GA")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # restaurant_name -> {score, mentions, upvotes, raw_mentions}
    restaurant_data = defaultdict(lambda: {
        'score': 0.0,
        'mentions': 0,
        'upvotes': 0,
        'raw_mentions': [],
    })

    seen_post_ids = set()
    total_posts = 0

    for subreddit in SUBREDDITS:
        print(f"\n── Scraping r/{subreddit} ──")
        for query in SEARCH_QUERIES:
            print(f"  Searching: '{query}'...", end=' ')
            posts = search_subreddit(subreddit, query, limit=25)
            print(f"{len(posts)} posts")
            time.sleep(1.2)  # Reddit rate limit — be polite

            for post in posts:
                post_id = post['id']
                if post_id in seen_post_ids:
                    continue
                seen_post_ids.add(post_id)
                total_posts += 1

                # Combine title + body
                full_text = post['title'] + ' ' + post['selftext']
                post_upvotes = max(1, post['score'])

                # Extract names from post text
                names = extract_restaurant_names(full_text)
                for name in names:
                    if is_skippable(name):
                        continue
                    ctx_score = score_context(full_text, name)
                    if ctx_score <= 0:
                        continue
                    norm = normalize_name(name)
                    restaurant_data[norm]['score'] += ctx_score * (1 + post_upvotes / 100)
                    restaurant_data[norm]['mentions'] += 1
                    restaurant_data[norm]['upvotes'] += post_upvotes
                    if name not in restaurant_data[norm]['raw_mentions']:
                        restaurant_data[norm]['raw_mentions'].append(name)

                # Fetch comments for high-engagement posts
                if post['num_comments'] > 5 and post['score'] > 3:
                    time.sleep(1.0)
                    comments = get_post_comments(post_id, subreddit, limit=40)
                    for comment in comments:
                        body = comment['body']
                        comment_upvotes = max(1, comment['score'])
                        names = extract_restaurant_names(body)
                        for name in names:
                            if is_skippable(name):
                                continue
                            ctx_score = score_context(body, name)
                            if ctx_score <= 0:
                                continue
                            norm = normalize_name(name)
                            restaurant_data[norm]['score'] += ctx_score * (1 + comment_upvotes / 50)
                            restaurant_data[norm]['mentions'] += 1
                            restaurant_data[norm]['upvotes'] += comment_upvotes
                            if name not in restaurant_data[norm]['raw_mentions']:
                                restaurant_data[norm]['raw_mentions'].append(name)

    print(f"\n── Processing {len(restaurant_data)} unique names from {total_posts} posts ──")

    # Build final output — filter low-signal noise
    results = []
    for norm_name, data in restaurant_data.items():
        if data['mentions'] < 2 and data['score'] < 3:
            continue  # Too little signal
        # Pick the most common capitalized variant as display name
        raw = data['raw_mentions']
        display_name = max(set(raw), key=raw.count) if raw else norm_name.title()

        results.append({
            'name': display_name,
            'normalized': norm_name,
            'nommad_score': round(data['score'], 2),
            'mentions': data['mentions'],
            'reddit_upvotes': data['upvotes'],
        })

    # Sort by score
    results.sort(key=lambda x: x['nommad_score'], reverse=True)

    output = {
        'generated': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': 'Reddit (r/Augusta, r/AugustaGA)',
        'metro': 'Augusta, GA',
        'count': len(results),
        'restaurants': results,
    }

    # Write JSON
    out_path = '/home/claude/nommad/local-intel.json'
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"\n✓ Wrote {len(results)} scored restaurants to local-intel.json")
    print("\nTop 20 by Nommad Score:")
    print("-" * 50)
    for r in results[:20]:
        print(f"  {r['nommad_score']:6.1f}  {r['mentions']:2}x  {r['name']}")

    return output

if __name__ == '__main__':
    run_scraper()
