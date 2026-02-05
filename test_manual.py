from lambda_function import summarize_with_ai, get_transcript_text
import os
from dotenv import load_dotenv

# Load your API Key
load_dotenv()

# --- INPUT YOUR VIDEO URL HERE ---
VIDEO_URL = "https://youtu.be/ZXpokqw2waY?si=Moshpy11MNArsnZs" 
# Example: https://www.youtube.com/watch?v=dQw4w9WgXcQ (The ID is dQw4w9WgXcQ)

def extract_video_id(url):
    if "v=" in url:
        return url.split("v=")[1].split("&")[0]
    elif "youtu.be" in url:
        return url.split("/")[-1].split("?")[0]
    return url # Assume it's already an ID

if __name__ == "__main__":
    video_id = extract_video_id(VIDEO_URL)
    print(f"--- TESTING VIDEO ID: {video_id} ---")
    
    # 1. Get Transcript
    print("1. Fetching transcript...")
    transcript = get_transcript_text(video_id)
    
    if transcript:
        print(f"   Success! Transcript length: {len(transcript)} characters.")
        
        # Save transcript to file
        with open('transcript.txt', 'w') as f:
            f.write(transcript)
        
        # 2. Summarize
        print("2. Sending to AI...")
        summary = summarize_with_ai(transcript, "Test Video Title")
        
        if summary:
            print("Summary Generation Complete")
            
            # Save summary to file
            with open('summary.txt', 'w') as f:
                f.write(summary)
        else:
            print("❌ Error: AI summarization failed.")
    else:
        print("❌ Error: Could not get transcript. Video might not have captions enabled.")