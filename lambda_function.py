import os
import tempfile
from datetime import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
import boto3
import requests
from google import genai
import re

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
    
    # Fake Email Service
    class MockSES:
        def send_email(self, Source, Destination, Message):
            print(f"\n--- [Mock Email SENT] ---")
            print(f"To: {Destination['ToAddresses']}")
            print(f"Subject: {Message['Subject']['Data']}")
            print(f"Body Preview: {Message['Body']['Html']['Data'][:500]}...")
            print("-------------------------\n")

    table = MockTable()
    ses = MockSES()
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

        # 6. Send Email
        email_body = f"<h2>{episode['title']}</h2><p><a href='{episode['link']}'>Listen to Episode</a></p><hr>{summary_html}"
        ses.send_email(
            Source=SENDER_EMAIL,
            Destination={'ToAddresses': recipients},
            Message={
                'Subject': {'Data': f"Sermon Summary: {episode['title']}"},
                'Body': {'Html': {'Data': email_body}}
            }
        )

        # 7. Save to DB
        table.put_item(Item={
            'episode_id': episode['id'],
            'title': episode['title'],
            'sermon_date': target_date.isoformat(),
            'processed_at': datetime.utcnow().isoformat()
        })

        processed_count += 1

    return {"statusCode": 200, "body": f"Processed {processed_count} sermon(s)."}

# --- LOCAL RUNNER ---
if __name__ == "__main__":
    # Triggers the handler manually when running on your machine
    lambda_handler(None, None)