from dotenv import load_dotenv
import os
import requests

load_dotenv()

key = os.getenv('GEMINI_API_KEY')
if not key:
    print("API key missing in .env")
    exit(1)

url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
response = requests.get(url)

if response.status_code == 200:
    models = response.json()
    print("Available models:")
    for model in models.get('models', []):
        print(f" - {model['name']}")
else:
    print(f"Error: {response.status_code} - {response.text}")