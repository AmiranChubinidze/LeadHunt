import json
import os
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from instagrapi import Client
import redis

_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)


def get_fernet():
    key = os.getenv("FERNET_KEY")
    if not key:
        raise ValueError("FERNET_KEY missing")
    return Fernet(key)


def encrypt_session(session_dict: dict) -> str:
    f = get_fernet()
    payload = json.dumps(session_dict).encode("utf-8")
    return f.encrypt(payload).decode("utf-8")


def decrypt_session(encrypted_text: str) -> dict:
    f = get_fernet()
    payload = f.decrypt(encrypted_text.encode("utf-8")).decode("utf-8")
    return json.loads(payload)


def get_redis():
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    try:
        client = redis.Redis.from_url(url, decode_responses=True)
        client.ping()
        return client
    except Exception:
        return None


def get_instagram_client(settings_dict: dict | None):
    client = Client()
    client.delay_range = [4, 12]
    if settings_dict:
        client.set_settings(settings_dict)
    return client


def ensure_sessions_dir():
    path = Path("sessions")
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_session_path(username: str) -> Path:
    safe = username.replace("@", "")
    return Path("sessions") / f"{safe}.enc"
