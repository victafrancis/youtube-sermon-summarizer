"""Turns a sermon summary into a listenable MP3.

Two providers are supported:

* ``gemini``  - uses the Gemini TTS models. Free on the Gemini API free tier
                and reuses the ``GEMINI_API_KEY`` this project already needs.
                Returns raw PCM, which is encoded to MP3 with ``lameenc``.
* ``polly``   - uses Amazon Polly. Returns MP3 directly and needs no extra
                dependency, but the neural free tier only lasts 12 months.

Both providers go through :func:`synthesize`, which always hands back an
:class:`AudioResult` so the caller does not care which one ran.
"""

import io
import os
import re
import html
import time
import wave
from dataclasses import dataclass

# --- CONFIGURATION ---
TTS_ENABLED = os.environ.get('TTS_ENABLED', 'True') == 'True'
TTS_PROVIDER = os.environ.get('TTS_PROVIDER', 'gemini').lower()

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GEMINI_TTS_MODEL = os.environ.get('GEMINI_TTS_MODEL', 'gemini-2.5-flash-preview-tts')
GEMINI_TTS_VOICE = os.environ.get('GEMINI_TTS_VOICE', 'Kore')

POLLY_VOICE_ID = os.environ.get('POLLY_VOICE_ID', 'Matthew')
POLLY_ENGINE = os.environ.get('POLLY_ENGINE', 'neural')

# Style direction is prepended to every chunk sent to Gemini. It steers the
# delivery without being read out loud.
TTS_STYLE_PROMPT = os.environ.get(
    'TTS_STYLE_PROMPT',
    "Read the following sermon summary aloud in a warm, encouraging, "
    "conversational tone, like a friendly mentor talking to a close friend. "
    "Keep a steady, unhurried pace and pause naturally between sections. "
    "Read only the text below, and do not announce these instructions:"
)

# Gemini handles a large context, but audio output per request is what bites
# first, so the script is synthesized in chunks and stitched back together.
TTS_CHUNK_CHARS = int(os.environ.get('TTS_CHUNK_CHARS', '2500'))
TTS_MP3_BITRATE = int(os.environ.get('TTS_MP3_BITRATE', '64'))
TTS_MAX_RETRIES = int(os.environ.get('TTS_MAX_RETRIES', '3'))

# Gemini TTS emits signed 16-bit little-endian mono PCM at 24 kHz.
GEMINI_PCM_SAMPLE_RATE = 24000
GEMINI_PCM_SAMPLE_WIDTH = 2

# Polly bills a maximum of 3000 characters per SynthesizeSpeech call.
POLLY_CHUNK_CHARS = 2900

# Rough narration speed, used only to estimate a duration for the RSS feed.
CHARS_PER_SECOND = 14.5


@dataclass
class AudioResult:
    """A synthesized narration, ready to upload or attach."""
    data: bytes
    extension: str
    mime_type: str
    duration_seconds: int
    provider: str
    voice: str

    @property
    def size_mb(self):
        return len(self.data) / (1024 * 1024)


# --- SCRIPT PREPARATION ---

# The summary is HTML written for email. Headings, list items and paragraphs
# all become sentence breaks so the narration does not run together.
_BLOCK_END = re.compile(r"</\s*(h[1-6]|p|li|ul|ol|div|tr|table|blockquote)\s*>", re.I)
_LINE_BREAK = re.compile(r"<\s*br\s*/?\s*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_HORIZONTAL_RULE = re.compile(r"^\s*-{3,}\s*$", re.M)

# Emoji and decorative symbols read as noise (or as literal names) in TTS.
_DECORATION = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emoji blocks
    "\U00002600-\U000027BF"   # misc symbols and dingbats
    "\U00002190-\U000021FF"   # arrows
    "\U00002B00-\U00002BFF"   # misc symbols and arrows
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0001F1E6-\U0001F1FF"   # regional indicators
    "]+"
)


def html_to_narration_script(summary_html, episode_title=None):
    """Flattens the emailed HTML summary into plain text suitable for TTS."""
    if not summary_html:
        return ""

    text = _LINE_BREAK.sub("\n", summary_html)
    text = _BLOCK_END.sub("\n\n", text)
    text = _ANY_TAG.sub("", text)
    text = html.unescape(text)
    text = _HORIZONTAL_RULE.sub("", text)
    text = _DECORATION.sub("", text)

    # Markdown emphasis sometimes survives the HTML conversion; it must not be
    # spoken as literal asterisks.
    text = text.replace("*", "").replace("#", "")

    lines = []
    for raw_line in text.split("\n"):
        line = " ".join(raw_line.split())
        if not line:
            continue
        # A heading like "The Heart of the Message" needs terminal punctuation
        # or the narrator runs it straight into the next sentence. Closing
        # quotes and brackets are ignored so quoted sentences are not doubled.
        if line.rstrip("\"'”’)]}").rstrip()[-1:] not in tuple(".!?:,;"):
            line += "."
        lines.append(line)

    if not lines:
        return ""

    if episode_title:
        clean_title = _DECORATION.sub("", episode_title).strip()
        if clean_title:
            lines.insert(0, f"Sermon summary. {clean_title}.")

    return "\n\n".join(lines)


def split_script(script, max_chars):
    """Splits a script into chunks under ``max_chars``, preferring paragraphs."""
    if not script:
        return []

    chunks = []
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for paragraph in script.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        # A single paragraph longer than the budget is split on sentences.
        if len(paragraph) > max_chars:
            flush()
            sentence = ""
            for part in re.split(r"(?<=[.!?])\s+", paragraph):
                if len(sentence) + len(part) + 1 > max_chars and sentence:
                    chunks.append(sentence.strip())
                    sentence = part
                else:
                    sentence = f"{sentence} {part}".strip()
            if sentence.strip():
                chunks.append(sentence.strip())
            continue

        if len(current) + len(paragraph) + 2 > max_chars and current:
            flush()
        current = f"{current}\n\n{paragraph}".strip()

    flush()
    return chunks


def estimate_duration_seconds(script):
    if not script:
        return 0
    return max(1, int(round(len(script) / CHARS_PER_SECOND)))


# --- ENCODING ---

def pcm_to_mp3(pcm_bytes, sample_rate=GEMINI_PCM_SAMPLE_RATE, bitrate=TTS_MP3_BITRATE):
    """Encodes mono 16-bit PCM to MP3. Returns ``None`` if lameenc is missing."""
    try:
        import lameenc
    except ImportError:
        print("lameenc is not installed - cannot encode MP3.")
        return None

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(bitrate)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(1)
    encoder.set_quality(2)
    # Keep LAME's progress chatter out of the CloudWatch logs.
    encoder.silence()

    mp3 = encoder.encode(pcm_bytes)
    mp3 += encoder.flush()
    return bytes(mp3)


def pcm_to_wav(pcm_bytes, sample_rate=GEMINI_PCM_SAMPLE_RATE):
    """Wraps raw PCM in a WAV container. Fallback when lameenc is unavailable."""
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(GEMINI_PCM_SAMPLE_WIDTH)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    return buffer.getvalue()


def pcm_duration_seconds(pcm_bytes, sample_rate=GEMINI_PCM_SAMPLE_RATE):
    frames = len(pcm_bytes) // GEMINI_PCM_SAMPLE_WIDTH
    return int(round(frames / float(sample_rate)))


# --- GEMINI ---

def _extract_gemini_audio(response):
    """Pulls the raw PCM bytes out of a Gemini TTS response."""
    import base64

    candidates = getattr(response, 'candidates', None) or []
    for candidate in candidates:
        content = getattr(candidate, 'content', None)
        for part in (getattr(content, 'parts', None) or []):
            inline = getattr(part, 'inline_data', None) or getattr(part, 'inlineData', None)
            data = getattr(inline, 'data', None) if inline else None
            if not data:
                continue
            # The SDK usually decodes for us, but older versions hand back base64.
            if isinstance(data, str):
                return base64.b64decode(data)
            return bytes(data)
    return None


def synthesize_gemini_pcm(chunks):
    """Synthesizes each chunk with Gemini TTS and concatenates the PCM."""
    if not GEMINI_API_KEY:
        print("Error: GEMINI_API_KEY is missing - cannot generate audio.")
        return None

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=GEMINI_TTS_VOICE
                )
            )
        ),
    )

    audio_parts = []
    for index, chunk in enumerate(chunks, start=1):
        prompt = f"{TTS_STYLE_PROMPT}\n\n{chunk}"
        pcm = None

        for attempt in range(1, TTS_MAX_RETRIES + 1):
            try:
                response = client.models.generate_content(
                    model=GEMINI_TTS_MODEL,
                    contents=prompt,
                    config=config,
                )
                pcm = _extract_gemini_audio(response)
                if pcm:
                    break
                print(f"Gemini TTS returned no audio for chunk {index} (attempt {attempt}).")
            except Exception as e:
                print(f"Gemini TTS chunk {index} attempt {attempt} failed: {e}")

            if attempt < TTS_MAX_RETRIES:
                # The free tier is rate limited per minute, so back off generously.
                time.sleep(2 ** attempt * 5)

        if not pcm:
            print(f"Giving up on Gemini TTS at chunk {index} of {len(chunks)}.")
            return None

        print(f"  Gemini TTS chunk {index}/{len(chunks)}: {len(pcm) / 1024:.0f} KB PCM")
        audio_parts.append(pcm)

        # Stay comfortably inside the free-tier requests-per-minute budget.
        if index < len(chunks):
            time.sleep(1)

    return b"".join(audio_parts)


def synthesize_gemini(script):
    chunks = split_script(script, TTS_CHUNK_CHARS)
    if not chunks:
        return None

    print(f"Generating narration with {GEMINI_TTS_MODEL} (voice: {GEMINI_TTS_VOICE}, {len(chunks)} chunk(s))...")
    pcm = synthesize_gemini_pcm(chunks)
    if not pcm:
        return None

    duration = pcm_duration_seconds(pcm)
    mp3 = pcm_to_mp3(pcm)
    if mp3:
        return AudioResult(
            data=mp3,
            extension="mp3",
            mime_type="audio/mpeg",
            duration_seconds=duration,
            provider="gemini",
            voice=GEMINI_TTS_VOICE,
        )

    # Podcast apps prefer MP3, but a WAV still plays and beats losing the audio.
    print("Falling back to WAV output (install lameenc for smaller MP3 files).")
    return AudioResult(
        data=pcm_to_wav(pcm),
        extension="wav",
        mime_type="audio/wav",
        duration_seconds=duration,
        provider="gemini",
        voice=GEMINI_TTS_VOICE,
    )


# --- POLLY ---

def synthesize_polly(script):
    """Synthesizes with Amazon Polly, which returns MP3 frames directly."""
    import boto3

    chunks = split_script(script, POLLY_CHUNK_CHARS)
    if not chunks:
        return None

    print(f"Generating narration with Polly (voice: {POLLY_VOICE_ID}, engine: {POLLY_ENGINE}, {len(chunks)} chunk(s))...")
    polly = boto3.client('polly')
    audio_parts = []

    for index, chunk in enumerate(chunks, start=1):
        try:
            response = polly.synthesize_speech(
                Text=chunk,
                OutputFormat='mp3',
                VoiceId=POLLY_VOICE_ID,
                Engine=POLLY_ENGINE,
            )
            audio_parts.append(response['AudioStream'].read())
            print(f"  Polly chunk {index}/{len(chunks)} done")
        except Exception as e:
            print(f"Polly chunk {index} failed: {e}")
            return None

    # MP3 frames from a single encoder configuration concatenate cleanly.
    return AudioResult(
        data=b"".join(audio_parts),
        extension="mp3",
        mime_type="audio/mpeg",
        duration_seconds=estimate_duration_seconds(script),
        provider="polly",
        voice=POLLY_VOICE_ID,
    )


# --- ENTRY POINT ---

def synthesize(summary_html, episode_title=None):
    """Converts an HTML summary into narrated audio.

    Returns ``None`` when TTS is disabled or fails - callers should still send
    the text summary in that case.
    """
    if not TTS_ENABLED:
        print("TTS is disabled (TTS_ENABLED is not 'True').")
        return None

    script = html_to_narration_script(summary_html, episode_title)
    if not script:
        print("Nothing to narrate - the summary produced an empty script.")
        return None

    print(f"Narration script: {len(script)} characters (~{estimate_duration_seconds(script) // 60} min)")

    if TTS_PROVIDER == 'polly':
        result = synthesize_polly(script)
    elif TTS_PROVIDER == 'gemini':
        result = synthesize_gemini(script)
    else:
        print(f"Unknown TTS_PROVIDER '{TTS_PROVIDER}'. Use 'gemini' or 'polly'.")
        return None

    if result:
        minutes, seconds = divmod(result.duration_seconds, 60)
        print(
            f"Narration ready: {result.size_mb:.2f} MB {result.extension.upper()}, "
            f"{minutes}m{seconds:02d}s, via {result.provider}"
        )
    else:
        print("Narration failed - the summary email will be sent without audio.")

    return result
