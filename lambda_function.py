import json
import os
from pydoc import text
import boto3
import requests
from youtube_transcript_api import YouTubeTranscriptApi
from botocore.exceptions import ClientError

# Load local secrets only if running locally
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- CONFIGURATION ---
# Converts the string 'True' in .env to a real Python Boolean
LOCAL_TEST_MODE = os.environ.get('LOCAL_TEST_MODE') == 'True'

YOUTUBE_CHANNEL_ID = os.environ.get('CHANNEL_ID')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL')
RECIPIENT_EMAIL = os.environ.get('RECIPIENT_EMAIL')
DYNAMODB_TABLE = os.environ.get('DYNAMO_TABLE', 'ProcessedVideos')

# --- MOCKING AWS (The "Local" Magic) ---
if LOCAL_TEST_MODE:
    print("⚠️  RUNNING IN LOCAL TEST MODE (No DB/Email actions) ⚠️")
    
    # Fake DynamoDB
    class MockTable:
        def get_item(self, Key): 
            # Return empty to pretend we haven't seen the video yet
            return {} 
        def put_item(self, Item): 
            print(f"[Mock DB] Saved Video ID: {Item['video_id']}")
    
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

def get_latest_video():
    """Fetches the latest video from the YouTube RSS feed."""
    if not YOUTUBE_CHANNEL_ID:
        print("Error: CHANNEL_ID is missing in .env")
        return None
        
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
    try:
        response = requests.get(rss_url)
        if response.status_code != 200:
            print(f"RSS Error: {response.status_code}")
            return None
        
        # Simple string parsing to avoid heavy XML libraries if possible, 
        # but XML ElementTree is safer.
        import xml.etree.ElementTree as ET
        root = ET.fromstring(response.content)
        ns = {'yt': 'http://www.w3.org/2005/Atom'}
        entry = root.find('yt:entry', ns)
        
        if entry:
            video_id = entry.find('yt:id', ns).text.replace('yt:video:', '')
            title = entry.find('yt:title', ns).text
            link = entry.find('yt:link', ns).attrib['href']
            return {"id": video_id, "title": title, "link": link}
    except Exception as e:
        print(f"Error fetching RSS: {e}")
    return None

def get_transcript_text(video_id):
    """Downloads the transcript."""
    try:
        transcript_list = YouTubeTranscriptApi().list(video_id)

        # Try English first
        try:
            transcript = transcript_list.find_transcript(['en']).fetch()
        except:
            # Fall back to any available language
            available_langs = (list(transcript_list._manually_created_transcripts.keys()) +
                             list(transcript_list._generated_transcripts.keys()))
            if available_langs:
                transcript = transcript_list.find_transcript([available_langs[0]]).fetch()
            else:
                raise Exception("No transcripts available in any language")

        full_text = " ".join([snippet.text for snippet in transcript.snippets])
        return full_text
    except Exception as e:
        print(f"Could not retrieve transcript (Video might not have captions): {e}")
        return None

def summarize_with_ai(text, video_title):
    """Sends the text to Gemini."""
    if not GEMINI_API_KEY:
        print("Error: GEMINI_API_KEY is missing in .env")
        return None
    
    model = "gemini-flash-latest"
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"

    with open('prompt.txt', 'r') as f:
        prompt_template = f.read()
    prompt = prompt_template.format(video_title=video_title, transcript=text)

    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            return response.json()['candidates'][0]['content']['parts'][0]['text']
        else:
            print(f"AI Error: {response.text}")
            return None
    except Exception as e:
        print(f"AI Connection Failed: {e}")
        return None

# --- MAIN HANDLER (The Controller) ---
def lambda_handler(event, context):
    print("Checking for new sermons...")
    
    # 1. Get latest video
    video = get_latest_video()
    if not video:
        return {"statusCode": 200, "body": "No videos found."}
    
    print(f"Found video: {video['title']}")
    
    # 2. Check Database
    # Note: in LOCAL_TEST_MODE, table.get_item always returns empty, so we proceed
    if 'Item' in table.get_item(Key={'video_id': video['id']}):
        print("Video already processed.")
        return {"statusCode": 200, "body": "Video already processed."}
    
    # 3. Get Transcript
    transcript = get_transcript_text(video['id'])
    if not transcript:
        return {"statusCode": 500, "body": "No transcript available."}
    
    # 4. Summarize
    print("Summarizing (this may take 10-20 seconds)...")
    summary_html = summarize_with_ai(transcript, video['title'])
    if not summary_html:
        return {"statusCode": 500, "body": "AI generation failed."}
    
    # 5. Send Email
    email_body = f"<h2>{video['title']}</h2><p><a href='{video['link']}'>Watch Video</a></p><hr>{summary_html}"
    ses.send_email(
        Source=SENDER_EMAIL,
        Destination={'ToAddresses': [RECIPIENT_EMAIL]},
        Message={
            'Subject': {'Data': f"Sermon Summary: {video['title']}"},
            'Body': {'Html': {'Data': email_body}}
        }
    )
    
    # 6. Save to DB
    table.put_item(Item={'video_id': video['id'], 'title': video['title']})
    
    return {"statusCode": 200, "body": "Success!"}

# --- LOCAL RUNNER ---
if __name__ == "__main__":
    # Triggers the handler manually when running on your machine
    lambda_handler(None, None)