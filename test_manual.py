from dotenv import load_dotenv
from lambda_function import download_audio, summarize_with_gemini_audio, clean_html_output

# Load your API Key
load_dotenv()

# --- INPUT YOUR PODCAST MP3 URL HERE ---
EPISODE_AUDIO_URL = "https://anchor.fm/s/15ae74cc/podcast/play/114542643/https%3A%2F%2Fd3ctxlq1ktw2nl.cloudfront.net%2Fstaging%2F2026-0-26%2Ff2e5812b-3ead-2214-4386-fea98839de96.mp3"
EPISODE_TITLE = "Test Episode Title"
LOCAL_AUDIO_FILENAME = "audio.mp3"

if __name__ == "__main__":
    print("--- TESTING PODCAST AUDIO ---")

    # 1. Download Audio
    print(f"1. Downloading audio for: {EPISODE_TITLE}...")
    audio_path = download_audio(EPISODE_AUDIO_URL, filename=LOCAL_AUDIO_FILENAME)

    if audio_path:
        print(f"   Success! Audio saved at: {audio_path}")

        # 2. Summarize
        print(f"2. Sending to AI for: {EPISODE_TITLE}...")
        summary = summarize_with_gemini_audio(audio_path, EPISODE_TITLE)

        if summary:
            print("Summary Generation Complete")

            # Save summary to file
            with open('summary.txt', 'w', encoding='utf-8') as f:
                f.write(clean_html_output(summary))
        else:
            print("❌ Error: AI summarization failed.")
    else:
        print("❌ Error: Could not download audio. Check the MP3 URL.")