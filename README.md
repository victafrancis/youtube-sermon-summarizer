# Sermon Summarizer (CCF YouTube)

AWS Lambda that checks the **latest CCF sermon** on YouTube, grabs the transcript, summarizes it with Gemini AI, and emails the summary to a configured recipient. A DynamoDB table is used to prevent re-processing the same video.

## What It Does
- Polls the YouTube RSS feed for the latest video on a specific channel (via EventBridge).
- Retrieves the transcript (auto or manually generated).
- Summarizes the sermon with Gemini.
- Emails the summary via AWS SES.
- Stores the video ID in DynamoDB so it only runs once per video.

## Architecture (High Level)
1. **YouTube RSS** → latest video metadata
2. **YouTube Transcript API** → full transcript text
3. **Gemini API** → summary generation
4. **AWS SES** → email delivery
5. **AWS DynamoDB** → deduplication of processed videos
6. **AWS EventBridge** → schedules the polling

## Prerequisites
- Python 3.11+
- AWS account with:
  - **Lambda**
  - **DynamoDB** table
  - **SES** verified sender + permissions
  - **EventBridge**
- Gemini API key

## Environment Variables
Create a `.env` file for local testing (or set Lambda environment variables):

```bash
LOCAL_TEST_MODE=True
CHANNEL_ID=YOUR_YOUTUBE_CHANNEL_ID
GEMINI_API_KEY=YOUR_GEMINI_API_KEY
SENDER_EMAIL=verified-ses-sender@example.com
RECIPIENT_EMAIL=recipient@example.com
DYNAMO_TABLE=ProcessedVideos
```

### Notes
- `LOCAL_TEST_MODE=True` enables local mocks for DynamoDB + SES.
- `DYNAMO_TABLE` defaults to `ProcessedVideos` if not provided.

## Install Dependencies
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Local Test (Lambda Flow)
Runs the full Lambda handler locally with mocked AWS services:

```bash
python lambda_function.py
```

## Manual Transcript/Summary Test
Use the helper script for testing a specific YouTube video:

```bash
python test_manual.py
```

Edit `VIDEO_URL` inside `test_manual.py` to test a different sermon.

## Deployment Notes
1. Deploy `lambda_function.py` and dependencies to AWS Lambda.
2. Configure Lambda environment variables listed above.
3. Ensure the Lambda role has permissions for:
   - `dynamodb:GetItem`
   - `dynamodb:PutItem`
   - `ses:SendEmail`
4. In SES, verify the sender email (and recipient if in the SES sandbox).

## Scheduling
This Lambda is intended to be triggered on a schedule using **Amazon EventBridge** (e.g., a cron rule that polls for the latest sermon).

## Files of Interest
- `lambda_function.py` — Lambda entry point and main logic
- `prompt.txt` — Prompt template for Gemini
- `test_manual.py` — Manual transcript + summary test
- `requirements.txt` — Python dependencies

## Troubleshooting
- **No transcript available**: the video may not have captions enabled.
- **SES errors**: ensure sender is verified and the Lambda role allows `ses:SendEmail`.
- **Already processed**: the video ID is stored in DynamoDB and won’t be reprocessed.

---
If you want the README expanded with a sample summary or diagrams, just let me know.