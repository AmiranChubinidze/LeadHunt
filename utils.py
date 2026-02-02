import hashlib
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


def get_proxy_session_id(ig_username: str | None) -> str | None:
    if not ig_username:
        return None
    session_path = get_base_dir() / "sessions" / f"__proxy_{ig_username.replace('@', '')}.txt"
    if session_path.exists():
        value = session_path.read_text(encoding="utf-8").strip()
        if value:
            return value
    digest = hashlib.sha1(ig_username.encode("utf-8")).hexdigest()[:12]
    session_path.write_text(digest, encoding="utf-8")
    return digest


def build_proxy_username(ig_username: str | None) -> str | None:
    proxy_user = os.getenv("PROXY_USER")
    if not proxy_user:
        return None
    template = os.getenv("PROXY_USER_TEMPLATE")
    if template:
        session_id = get_proxy_session_id(ig_username) or ""
        return (
            template.replace("{user}", proxy_user)
            .replace("{session}", session_id)
            .replace("{ig}", ig_username or "")
            .replace("{country}", os.getenv("PROXY_COUNTRY", ""))
            .replace("{region}", os.getenv("PROXY_REGION", ""))
            .replace("{city}", os.getenv("PROXY_CITY", ""))
        )
    session_id = os.getenv("PROXY_SESSION") or get_proxy_session_id(ig_username)
    if session_id:
        return f"{proxy_user}-session-{session_id}"
    return proxy_user


def get_instagram_client(settings_dict: dict | None, ig_username: str | None = None):
    client = Client()
    client.delay_range = [4, 12]
    proxy_host = os.getenv("PROXY_HOST")
    proxy_port = os.getenv("PROXY_PORT")
    proxy_user = build_proxy_username(ig_username)
    proxy_pass = os.getenv("PROXY_PASS")
    if proxy_host and proxy_port and proxy_user and proxy_pass:
        client.set_proxy(f"http://{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}")
    if settings_dict:
        client.set_settings(settings_dict)
    return client


def get_base_dir() -> Path:
    return Path(__file__).resolve().parent


def ensure_sessions_dir():
    path = get_base_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_session_path(username: str) -> Path:
    safe = username.replace("@", "")
    return get_base_dir() / "sessions" / f"{safe}.enc"
