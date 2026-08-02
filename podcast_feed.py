"""Publishes narrated summaries as a private podcast feed on S3.

The Lambda uploads each MP3 to S3 and rewrites ``feed.xml`` from the episodes
recorded in DynamoDB. Subscribing to that feed URL in any podcast app means new
summaries show up automatically - including in the car over CarPlay or Android
Auto - without touching email.

The feed is not listed in any directory. Its privacy comes from an unguessable
``AUDIO_PREFIX``, because podcast apps cannot authenticate or follow expiring
presigned URLs.
"""

import hashlib
import os
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape, quoteattr

# --- CONFIGURATION ---
AUDIO_BUCKET = os.environ.get('AUDIO_BUCKET')
# Treat this like a password: anyone with the full URL can read the feed.
AUDIO_PREFIX = os.environ.get('AUDIO_PREFIX', 'sermon-audio').strip('/')
# Set when serving through CloudFront or a custom domain instead of S3 directly.
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', '').rstrip('/')

FEED_TITLE = os.environ.get('FEED_TITLE', 'Sermon Summaries')
FEED_AUTHOR = os.environ.get('FEED_AUTHOR', 'Automated Sermon Summarizer')
FEED_DESCRIPTION = os.environ.get(
    'FEED_DESCRIPTION',
    'AI-narrated summaries of the weekly sermon, so you can catch the message on the drive.'
)
FEED_LINK = os.environ.get('FEED_LINK', '')
FEED_IMAGE_URL = os.environ.get('FEED_IMAGE_URL', '')
FEED_LANGUAGE = os.environ.get('FEED_LANGUAGE', 'en-us')
# Plain text - it is XML-escaped at render time.
FEED_CATEGORY = os.environ.get('FEED_CATEGORY', 'Religion & Spirituality')
FEED_MAX_ITEMS = int(os.environ.get('FEED_MAX_ITEMS', '50'))

FEED_FILENAME = 'feed.xml'
ITUNES_NS = 'http://www.itunes.com/dtds/podcast-1.0.dtd'


def is_configured():
    return bool(AUDIO_BUCKET)


def _s3_client():
    import boto3
    return boto3.client('s3')


def _region():
    return os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'us-east-1'


def public_url(key):
    """Builds the publicly readable URL for an object under the secret prefix."""
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/{key}"
    region = _region()
    if region == 'us-east-1':
        return f"https://{AUDIO_BUCKET}.s3.amazonaws.com/{key}"
    return f"https://{AUDIO_BUCKET}.s3.{region}.amazonaws.com/{key}"


def audio_key(episode_id, sermon_date, extension='mp3'):
    """A stable, collision-free key derived from the episode id."""
    digest = hashlib.sha256(episode_id.encode('utf-8')).hexdigest()[:12]
    return f"{AUDIO_PREFIX}/audio/{sermon_date}-{digest}.{extension}"


def feed_key():
    return f"{AUDIO_PREFIX}/{FEED_FILENAME}"


def feed_url():
    return public_url(feed_key())


def upload_audio(audio_result, key):
    """Uploads the narration to S3 and returns its public URL."""
    if not is_configured():
        print("AUDIO_BUCKET is not set - skipping S3 upload.")
        return None

    try:
        _s3_client().put_object(
            Bucket=AUDIO_BUCKET,
            Key=key,
            Body=audio_result.data,
            ContentType=audio_result.mime_type,
            # Episode audio never changes, so let players cache it hard.
            CacheControl='public, max-age=31536000',
        )
    except Exception as e:
        print(f"S3 upload failed for {key}: {e}")
        return None

    url = public_url(key)
    print(f"Uploaded narration to S3: {key} ({audio_result.size_mb:.2f} MB)")
    return url


# --- FEED GENERATION ---

def _format_duration(seconds):
    seconds = int(seconds or 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _parse_item_datetime(item):
    """Best-effort publish timestamp for an episode record."""
    published_at = item.get('published_at')
    if published_at:
        try:
            parsed = datetime.fromisoformat(published_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass

    # Older records predate published_at and only carry the sermon date.
    sermon_date = item.get('sermon_date')
    if sermon_date:
        try:
            return datetime.fromisoformat(sermon_date).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    return datetime.now(timezone.utc)


def _cdata(text):
    # A literal ']]>' inside the payload would close the section early.
    safe = (text or "").replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{safe}]]>"


def build_feed_xml(items):
    """Renders an RSS 2.0 podcast feed from DynamoDB episode records."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<rss version="2.0" xmlns:itunes="{ITUNES_NS}">',
        '  <channel>',
        f'    <title>{escape(FEED_TITLE)}</title>',
        f'    <description>{escape(FEED_DESCRIPTION)}</description>',
        f'    <language>{escape(FEED_LANGUAGE)}</language>',
        f'    <itunes:author>{escape(FEED_AUTHOR)}</itunes:author>',
        '    <itunes:explicit>false</itunes:explicit>',
        f'    <itunes:category text={quoteattr(FEED_CATEGORY)}/>',
        f'    <lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>',
    ]

    if FEED_LINK:
        lines.append(f'    <link>{escape(FEED_LINK)}</link>')
    if FEED_IMAGE_URL:
        lines.append(f'    <itunes:image href={quoteattr(FEED_IMAGE_URL)}/>')

    for item in items:
        url = item.get('audio_url')
        if not url:
            continue

        title = item.get('title') or 'Sermon Summary'
        published = format_datetime(_parse_item_datetime(item))
        length = int(item.get('audio_bytes') or 0)
        mime = item.get('audio_mime') or 'audio/mpeg'
        duration = _format_duration(item.get('audio_duration_sec'))
        summary_html = item.get('summary_html') or ''
        episode_link = item.get('episode_link') or ''

        lines.extend([
            '    <item>',
            f'      <title>{escape(title)}</title>',
            f'      <description>{_cdata(summary_html)}</description>',
            f'      <pubDate>{published}</pubDate>',
            f'      <guid isPermaLink="false">{escape(str(item.get("episode_id", url)))}</guid>',
            f'      <enclosure url={quoteattr(url)} length="{length}" type={quoteattr(mime)}/>',
            f'      <itunes:duration>{duration}</itunes:duration>',
        ])
        if episode_link:
            lines.append(f'      <link>{escape(episode_link)}</link>')
        lines.append('    </item>')

    lines.extend(['  </channel>', '</rss>', ''])
    return "\n".join(lines)


def _scan_episodes(table):
    """Reads every episode record, following DynamoDB pagination."""
    items = []
    kwargs = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return items


def rebuild_feed(table):
    """Regenerates feed.xml from DynamoDB and uploads it to S3."""
    if not is_configured():
        print("AUDIO_BUCKET is not set - skipping podcast feed rebuild.")
        return None

    try:
        records = _scan_episodes(table)
    except Exception as e:
        print(f"Could not read episodes for the feed: {e}")
        return None

    episodes = [record for record in records if record.get('audio_url')]
    episodes.sort(key=_parse_item_datetime, reverse=True)
    episodes = episodes[:FEED_MAX_ITEMS]

    xml = build_feed_xml(episodes)

    try:
        _s3_client().put_object(
            Bucket=AUDIO_BUCKET,
            Key=feed_key(),
            Body=xml.encode('utf-8'),
            ContentType='application/rss+xml; charset=utf-8',
            # Podcast apps must always see the newest episode list.
            CacheControl='no-cache, max-age=60',
        )
    except Exception as e:
        print(f"Feed upload failed: {e}")
        return None

    url = feed_url()
    print(f"Podcast feed rebuilt with {len(episodes)} episode(s): {url}")
    return url
