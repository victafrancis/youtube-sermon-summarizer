import os
import tempfile
from datetime import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
import boto3
import requests
from google import genai
import re

import tts
import podcast_feed

# Load local secrets only if running locally
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- CONFIGURATION ---
# Converts the string 'True' in .env to a real Python Boolean
LOCAL_TEST_MODE = os.environ.get('LOCAL_TEST_MODE') == 'True'
FORCE_DATE = os.environ.get('FORCE_DATE')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')
PODCAST_RSS_URL = os.environ.get('PODCAST_RSS_URL')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL')
RECIPIENT_EMAIL = os.environ.get('RECIPIENT_EMAIL')
RECIPIENT_EMAILS = os.environ.get('RECIPIENT_EMAILS')
DYNAMODB_TABLE = os.environ.get('DYNAMO_TABLE', 'CCFProcessedAudio')
# Attach the narration to the email as well as linking it. Handy if you have no
# S3 bucket, but the file inflates the message by about a third in transit.
ATTACH_AUDIO_TO_EMAIL = os.environ.get('ATTACH_AUDIO_TO_EMAIL') == 'True'

# --- MOCKING AWS (The "Local" Magic) ---
if LOCAL_TEST_MODE:
    print("⚠️  RUNNING IN LOCAL TEST MODE (No DB/Email actions) ⚠️")
    
    # Fake DynamoDB
    class MockTable:
        def __init__(self):
            self.items = {}
        def get_item(self, Key):
            item = self.items.get(Key['episode_id'])
            return {'Item': item} if item else {}
        def put_item(self, Item):
            self.items[Item['episode_id']] = Item
            print(f"[Mock DB] Saved Episode ID: {Item['episode_id']}")
        def scan(self, **kwargs):
            return {'Items': list(self.items.values())}

    # Fake Email Service
    class MockSES:
        def send_email(self, Source, Destination, Message):
            print(f"\n--- [Mock Email SENT] ---")
            print(f"To: {Destination['ToAddresses']}")
            print(f"Subject: {Message['Subject']['Data']}")
            print(f"Body Preview: {Message['Body']['Html']['Data'][:500]}...")
            print("-------------------------\n")
        def send_raw_email(self, Source, Destinations, RawMessage):
            print(f"\n--- [Mock Email SENT with attachment] ---")
            print(f"To: {Destinations}")
            print(f"Raw size: {len(RawMessage['Data']) / (1024 * 1024):.2f} MB")
            print("-----------------------------------------\n")

    # Fake S3 - writes where the real code would upload so you can play the
    # MP3 and inspect feed.xml locally.
    class MockS3:
        def put_object(self, Bucket, Key, Body, **kwargs):
            path = os.path.join('local_output', Key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(Body if isinstance(Body, bytes) else Body.encode('utf-8'))
            print(f"[Mock S3] Wrote {path} ({len(Body) / 1024:.0f} KB)")

    table = MockTable()
    ses = MockSES()
    podcast_feed._s3_client = lambda: MockS3()
    # Give the feed a bucket name so the local run exercises the real code path.
    if not podcast_feed.AUDIO_BUCKET:
        podcast_feed.AUDIO_BUCKET = 'local-test-bucket'
else:
    # Real AWS Resources (Runs only when uploaded to Lambda)
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(DYNAMODB_TABLE)
    ses = boto3.client('ses')

# --- CORE LOGIC ---

def parse_rfc2822(value):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def get_target_date(timezone=ZoneInfo("America/New_York")):
    if FORCE_DATE:
        try:
            return datetime.strptime(FORCE_DATE, "%Y-%m-%d").date()
        except ValueError:
            print("Invalid FORCE_DATE. Use YYYY-MM-DD.")
            return None
    return datetime.now(timezone).date()


def get_recent_episodes(limit=20):
    """Fetches recent episodes from the podcast RSS feed."""
    if not PODCAST_RSS_URL:
        print("Error: PODCAST_RSS_URL is missing in .env")
        return None

    try:
        response = requests.get(PODCAST_RSS_URL, timeout=30)
        if response.status_code != 200:
            print(f"RSS Error: {response.status_code}")
            return None

        import xml.etree.ElementTree as ET
        root = ET.fromstring(response.content)
        items = root.findall('./channel/item')

        episodes = []
        for item in items[:limit]:
            title_elem = item.find('title')
            link_elem = item.find('link')
            pub_elem = item.find('pubDate')
            guid_elem = item.find('guid')
            enclosure_elem = item.find('enclosure')

            title = title_elem.text if title_elem is not None else None
            link = link_elem.text if link_elem is not None else None
            published_at = parse_rfc2822(pub_elem.text if pub_elem is not None else None)
            audio_url = enclosure_elem.attrib.get('url') if enclosure_elem is not None else None
            episode_id = (guid_elem.text if guid_elem is not None and guid_elem.text else None) or link or audio_url

            if episode_id and title and audio_url and published_at:
                episodes.append({
                    "id": episode_id,
                    "title": title,
                    "link": link,
                    "published_at": published_at,
                    "audio_url": audio_url
                })

        return episodes
    except Exception as e:
        print(f"Error fetching RSS: {e}")
    return None


def get_recipient_emails():
    raw = RECIPIENT_EMAILS or RECIPIENT_EMAIL or ""
    return [email.strip() for email in raw.split(',') if email.strip()]


def is_sermon_for_date(episode, target_date, timezone=ZoneInfo("America/New_York")):
    published_at = episode['published_at']
    if not published_at:
        return False
    local_date = published_at.astimezone(timezone).date()
    return local_date == target_date


def get_temp_dir():
    return "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()


def download_audio(url, filename="sermon.mp3"):
    if not url:
        return None
    temp_dir = get_temp_dir()
    file_path = os.path.join(temp_dir, filename)

    try:
        with requests.get(url, stream=True, timeout=60) as response:
            response.raise_for_status()
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        return file_path
    except Exception as e:
        print(f"Error downloading audio: {e}")
        return None


def get_genai_client():
    return genai.Client(api_key=GEMINI_API_KEY)


def summarize_with_gemini_audio(audio_path, episode_title, mime_type="audio/mpeg"):
    """Uploads the audio to Gemini and requests a summary."""
    if not GEMINI_API_KEY:
        print("Error: GEMINI_API_KEY is missing in .env")
        return None

    file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
    print(f"Uploading audio to Gemini: {os.path.basename(audio_path)} ({file_size_mb:.2f} MB, {mime_type})")

    client = get_genai_client()
    try:
        uploaded = client.files.upload(file=audio_path)
    except Exception as e:
        print(f"Gemini upload failed: {e}")
        return None

    with open('prompt.txt', 'r', encoding='utf-8') as f:
        prompt_template = f.read()
    prompt = prompt_template.format(
        title=episode_title,
        transcript=(
            "Use the attached audio file as the source. "
            "Summarize directly from the audio."
        )
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt, uploaded]
        )
        return response.text
    except Exception as e:
        print(f"AI Connection Failed: {e}")
        return None
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception as e:
            print(f"Gemini cleanup failed: {e}")


def clean_html_output(summary_html):
    """Converts simple Markdown emphasis to HTML and strips stray asterisks."""
    if not summary_html:
        return summary_html

    # Convert **bold** to <strong>bold</strong>
    summary_html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", summary_html)

    # Convert *italic* to <em>italic</em>
    summary_html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", summary_html)

    # Remove any remaining stray asterisks
    summary_html = summary_html.replace("*", "")

    return summary_html

def format_duration(seconds):
    minutes, secs = divmod(int(seconds or 0), 60)
    return f"{minutes} min {secs:02d} sec"


def build_audio_section(audio_result, audio_url, feed_link):
    """The 'listen instead of read' block that sits above the summary."""
    if not audio_result:
        return ""

    if audio_url:
        headline = (
            f"<a href=\"{audio_url}\" style=\"font-size: 15px !important; font-weight: bold;\">"
            f"&#127911; Listen to this summary</a>"
            f" <span style=\"color: #555;\">({format_duration(audio_result.duration_seconds)})</span>"
        )
    else:
        headline = (
            "<span style=\"font-size: 15px !important; font-weight: bold;\">"
            f"&#127911; Audio summary attached ({format_duration(audio_result.duration_seconds)})</span>"
        )

    subscribe = ""
    if feed_link:
        subscribe = (
            "<div style=\"margin: 8px 0 0; font-size: 13px !important; color: #555;\">"
            "Listening in the car? Subscribe once in your podcast app and every "
            f"summary shows up automatically:<br><a href=\"{feed_link}\">{feed_link}</a>"
            "</div>"
        )

    return (
        "<div style=\"background: #f4f7fb; border-radius: 8px; padding: 14px 16px;"
        " margin: 0 0 18px;\">"
        f"{headline}{subscribe}"
        "</div>"
    )


def build_email_body(episode, summary_html, audio_section):
    return (
        "<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\""
        " style=\"border-collapse: collapse; font-family: Arial, sans-serif;\">"
        "<tr>"
        "<td style=\"padding: 0; font-size: 15px !important; line-height: 1.6;\">"
        f"<h2 style=\"margin: 0 0 12px; font-size: 20px !important; line-height: 1.3;\">{episode['title']}</h2>"
        f"{audio_section}"
        f"<p style=\"margin: 0 0 16px; font-size: 15px !important;\">"
        f"<a href='{episode['link']}' style=\"font-size: 15px !important;\">Listen to Episode</a>"
        "</p>"
        "<hr style=\"margin: 16px 0;\">"
        f"<div style=\"font-size: 15px !important; line-height: 1.6;\">{summary_html}</div>"
        "</td>"
        "</tr>"
        "</table>"
    )


def send_summary_email(recipients, subject, body_html, audio_result=None, filename=None):
    """Sends the summary, attaching the narration when asked to."""
    if not (audio_result and ATTACH_AUDIO_TO_EMAIL):
        response = ses.send_email(
            Source=SENDER_EMAIL,
            Destination={'ToAddresses': recipients},
            Message={
                'Subject': {'Data': subject},
                'Body': {'Html': {'Data': body_html}}
            }
        )
        return response.get('MessageId') if isinstance(response, dict) else None

    # SES only accepts attachments through the raw (MIME) send path.
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.audio import MIMEAudio

    message = MIMEMultipart('mixed')
    message['Subject'] = subject
    message['From'] = SENDER_EMAIL
    message['To'] = ', '.join(recipients)

    body_part = MIMEMultipart('alternative')
    body_part.attach(MIMEText(body_html, 'html', 'utf-8'))
    message.attach(body_part)

    subtype = 'mpeg' if audio_result.extension == 'mp3' else audio_result.extension
    attachment = MIMEAudio(audio_result.data, _subtype=subtype)
    attachment.add_header('Content-Disposition', 'attachment', filename=filename)
    message.attach(attachment)

    response = ses.send_raw_email(
        Source=SENDER_EMAIL,
        Destinations=recipients,
        RawMessage={'Data': message.as_bytes()}
    )
    return response.get('MessageId') if isinstance(response, dict) else None


def safe_filename(title, extension):
    """A tidy, filesystem-safe attachment name derived from the episode title."""
    cleaned = re.sub(r"[^\w\s-]", "", title or "sermon-summary").strip()
    cleaned = re.sub(r"[-\s]+", "-", cleaned) or "sermon-summary"
    return f"{cleaned[:60]}.{extension}"


# --- MAIN HANDLER (The Controller) ---
def lambda_handler(event, context):
    print("Checking for new sermons...")
    recipients = get_recipient_emails()
    if not recipients:
        return {"statusCode": 500, "body": "No recipients configured."}
    
    target_date = get_target_date(ZoneInfo("America/New_York"))
    if not target_date:
        return {"statusCode": 500, "body": "Invalid FORCE_DATE."}

    # 1. Get recent podcast episodes
    episodes = get_recent_episodes()
    if not episodes:
        return {"statusCode": 200, "body": "No episodes found."}

    # 2. Filter sermons for the trigger date
    matching_episodes = [
        episode for episode in episodes
        if is_sermon_for_date(episode, target_date)
    ]

    if not matching_episodes:
        return {"statusCode": 200, "body": "No sermons found for target date."}

    matching_episodes.sort(key=lambda e: e['published_at'], reverse=True)

    processed_count = 0
    for episode in matching_episodes:
        if processed_count >= 2:
            break

        # 3. Check Database
        # Note: in LOCAL_TEST_MODE, table.get_item uses in-memory items
        if 'Item' in table.get_item(Key={'episode_id': episode['id']}):
            print(f"Episode already processed: {episode['title']}")
            continue

        print(f"Processing episode: {episode['title']}")

        # 4. Download Audio
        audio_path = download_audio(episode['audio_url'], filename=f"{processed_count + 1}.mp3")
        if not audio_path:
            print("No audio available.")
            continue

        audio_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        print(
            f"Audio summary -> Title: {episode['title']} | "
            f"Size: {audio_size_mb:.2f} MB | Model: {GEMINI_MODEL}"
        )

        # 5. Summarize
        print("Summarizing (this may take 10-20 seconds)...")
        summary_html = summarize_with_gemini_audio(audio_path, episode['title'])
        if not summary_html:
            print("AI generation failed.")
            continue

        summary_html = clean_html_output(summary_html)

        # 6. Narrate the summary so it can be listened to instead of read.
        # A failure here must not cost us the text summary, so it degrades
        # to the original email-only behaviour.
        audio_result = None
        audio_url = None
        audio_filename = None
        try:
            audio_result = tts.synthesize(summary_html, episode['title'])
        except Exception as e:
            print(f"Narration failed unexpectedly: {e}")

        if audio_result:
            audio_filename = safe_filename(episode['title'], audio_result.extension)
            key = podcast_feed.audio_key(
                episode['id'], target_date.isoformat(), audio_result.extension
            )
            audio_url = podcast_feed.upload_audio(audio_result, key)

        # 7. Send Email
        feed_link = podcast_feed.feed_url() if podcast_feed.is_configured() else None
        email_body = build_email_body(
            episode,
            summary_html,
            build_audio_section(audio_result, audio_url, feed_link),
        )
        print(f"Sending email via SES to: {recipients} | From: {SENDER_EMAIL}")
        try:
            message_id = send_summary_email(
                recipients,
                f"CCF Sunday Sermon Summary: {episode['title']}",
                email_body,
                audio_result,
                audio_filename,
            )
            print(f"SES send succeeded. MessageId: {message_id}")
        except Exception as e:
            print(f"SES send failed: {e}")
            raise

        # 8. Save to DB (also the source of truth for the podcast feed)
        record = {
            'episode_id': episode['id'],
            'title': episode['title'],
            'sermon_date': target_date.isoformat(),
            'processed_at': datetime.utcnow().isoformat(),
            'episode_link': episode['link'],
            'published_at': episode['published_at'].isoformat(),
            'summary_html': summary_html,
        }
        if audio_url:
            record.update({
                'audio_url': audio_url,
                'audio_bytes': len(audio_result.data),
                'audio_duration_sec': audio_result.duration_seconds,
                'audio_mime': audio_result.mime_type,
                'audio_provider': audio_result.provider,
            })
        table.put_item(Item=record)

        processed_count += 1

    # 9. Refresh the podcast feed so subscribed apps pick up new episodes.
    if processed_count and podcast_feed.is_configured():
        try:
            podcast_feed.rebuild_feed(table)
        except Exception as e:
            print(f"Podcast feed rebuild failed: {e}")

    return {"statusCode": 200, "body": f"Processed {processed_count} sermon(s)."}

# --- LOCAL RUNNER ---
if __name__ == "__main__":
    # Triggers the handler manually when running on your machine
    lambda_handler(None, None)