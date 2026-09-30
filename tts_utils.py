import tempfile
from urllib.parse import quote

import requests


class ElevenLabsError(RuntimeError):
    pass


def generate_elevenlabs_audio(
    text: str,
    api_key: str,
    voice_id: str,
    model_id: str = "eleven_multilingual_v2",
    stability: float = 0.5,
    similarity_boost: float = 0.75,
    style: float = 0.0,
) -> str:
    """Generate MP3 audio from ElevenLabs and return a temp file path."""
    clean_key = api_key.strip()
    clean_voice_id = voice_id.strip()
    safe_voice_id = quote(clean_voice_id, safe="")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{safe_voice_id}"
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": clean_key,
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
        },
    }

    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=120,
        )
    except requests.RequestException as exc:
        raise ElevenLabsError(
            f"ElevenLabs request failed ({type(exc).__name__})"
        ) from exc

    if response.status_code != 200:
        body = response.text[:300].replace("\n", " ").strip()
        raise ElevenLabsError(
            f"ElevenLabs error {response.status_code}: {body}"
        )

    temp_audio = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    temp_audio.close()

    with open(temp_audio.name, "wb") as f:
        f.write(response.content)

    return temp_audio.name


def list_voices(api_key: str) -> list[dict]:
    """Return [{'voice_id': ..., 'name': ...}, ...] for the account's voices."""
    clean_key = api_key.strip()

    try:
        resp = requests.get(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": clean_key},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise ElevenLabsError(
            f"ElevenLabs request failed ({type(exc).__name__})"
        ) from exc

    if not resp.ok:
        body = resp.text[:300].replace("\n", " ").strip()
        raise ElevenLabsError(
            f"ElevenLabs error {resp.status_code}: {body}"
        )

    return resp.json().get("voices", [])
