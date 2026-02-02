import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from instagrapi.exceptions import ChallengeRequired, ClientLoginRequired, ClientThrottledError, LoginRequired
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from schemas import ConnectIGRequest, ConnectIGResponse, SearchResponse, SearchResult
from utils import decrypt_session, encrypt_session, ensure_sessions_dir, get_instagram_client, get_redis, get_session_path


HASHTAG_MAP = {
    "fitness": ["fitness", "workout", "gym"],
    "beauty": ["beauty", "skincare", "makeup"],
    "travel": ["travel", "wanderlust", "travelgram"],
    "food": ["food", "foodie", "foodporn"],
}

MOCK_IG_USERNAME = "demo.ighandle"
MOCK_IG_PASSWORD = "demo_pass_123"

limiter = Limiter(key_func=lambda request: f"{get_remote_address(request)}:{getattr(request.state, 'user_id', 'anon')}")


_memory_cap = {}
_pause_until = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_sessions_dir()
    yield


app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})


@app.middleware("http")
async def mock_auth_middleware(request: Request, call_next):
    request.state.user_id = 1
    request.state.ig_username = load_last_username(1)
    return await call_next(request)


def handle_ig_exception(exc: Exception):
    if isinstance(exc, ClientThrottledError):
        raise HTTPException(status_code=429, detail="Instagram rate limit")
    if isinstance(exc, ChallengeRequired):
        raise HTTPException(status_code=401, detail="Instagram challenge required")
    if isinstance(exc, ClientLoginRequired):
        raise HTTPException(status_code=401, detail="Instagram login required")
    if isinstance(exc, LoginRequired):
        raise HTTPException(status_code=401, detail="Instagram login required")


def is_retryable_server_error(exc: Exception) -> bool:
    msg = str(exc)
    return "Server Error" in msg or "status code 5" in msg


def login_with_retry(client, username: str, password: str, retries: int = 2):
    attempt = 0
    while True:
        try:
            return client.login(username, password)
        except Exception as exc:
            if is_retryable_server_error(exc) and attempt < retries:
                attempt += 1
                time.sleep(2 * attempt)
                continue
            raise


def enforce_internal_cap(user_id: int):
    redis_client = get_redis()
    hour_key = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    day_key = datetime.now(timezone.utc).strftime("%Y%m%d")
    hour_key_name = f"cap:{user_id}:{hour_key}"
    day_key_name = f"capday:{user_id}:{day_key}"
    if redis_client:
        hour_count = redis_client.incr(hour_key_name)
        if hour_count == 1:
            redis_client.expire(hour_key_name, 3600)
        if hour_count > 60:
            raise HTTPException(status_code=429, detail="Hourly request cap exceeded")
        day_count = redis_client.incr(day_key_name)
        if day_count == 1:
            redis_client.expire(day_key_name, 86400)
        if day_count > 200:
            raise HTTPException(status_code=429, detail="Daily request cap exceeded")
        return
    hour_count = _memory_cap.get(hour_key_name, 0) + 1
    _memory_cap[hour_key_name] = hour_count
    if hour_count > 60:
        raise HTTPException(status_code=429, detail="Hourly request cap exceeded")
    day_count = _memory_cap.get(day_key_name, 0) + 1
    _memory_cap[day_key_name] = day_count
    if day_count > 200:
        raise HTTPException(status_code=429, detail="Daily request cap exceeded")


def is_paused(user_id: int) -> bool:
    redis_client = get_redis()
    key = f"pause:{user_id}"
    if redis_client:
        return redis_client.exists(key) == 1
    now_ts = datetime.now(timezone.utc).timestamp()
    until_ts = _pause_until.get(key, 0)
    return until_ts > now_ts


def pause_user(user_id: int):
    redis_client = get_redis()
    key = f"pause:{user_id}"
    if redis_client:
        redis_client.setex(key, 86400, "1")
        return
    _pause_until[key] = datetime.now(timezone.utc).timestamp() + 86400


def get_hashtag_medias(client, tag: str, amount: int):
    def is_media_validation_error(err: Exception) -> bool:
        msg = str(err)
        return (
            ("validation error" in msg and "Media" in msg)
            or "clips_metadata" in msg
            or "audio_filter_infos" in msg
            or "image_versions2" in msg
            or "scans_profile" in msg
        )

    methods = [
        ("hashtag_medias_recent", {}),
        ("hashtag_medias_recent_v1", {}),
        ("hashtag_medias_v1", {"tab_key": "recent"}),
        ("hashtag_medias_v1", {"tab_key": "top"}),
        ("hashtag_medias_top_v1", {}),
    ]
    saw_validation_error = False
    for name, kwargs in methods:
        if not hasattr(client, name):
            continue
        try:
            return getattr(client, name)(tag, amount=amount, **kwargs)
        except Exception as exc:
            if is_media_validation_error(exc):
                saw_validation_error = True
                continue
            raise
    if saw_validation_error:
        return []
    return []


def get_media_user_pk(media):
    if hasattr(media, "user") and hasattr(media.user, "pk"):
        return media.user.pk
    if isinstance(media, dict):
        user = media.get("user") or {}
        return user.get("pk")
    return None


def get_media_like_count(media):
    if hasattr(media, "like_count"):
        return media.like_count
    if isinstance(media, dict):
        return media.get("like_count")
    return 0


def get_media_comment_count(media):
    if hasattr(media, "comment_count"):
        return media.comment_count
    if isinstance(media, dict):
        return media.get("comment_count")
    return 0


def get_user_map_path(user_id: int) -> Path:
    return ensure_sessions_dir() / f"__user_{user_id}.txt"


def save_last_username(user_id: int, username: str):
    path = get_user_map_path(user_id)
    path.write_text(username, encoding="utf-8")


def load_last_username(user_id: int) -> str | None:
    path = get_user_map_path(user_id)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def get_latest_session_username() -> str | None:
    sessions_dir = ensure_sessions_dir()
    candidates = [p for p in sessions_dir.glob("*.enc") if p.is_file()]
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return latest.stem or None


@app.get("/", response_class=HTMLResponse)
def index():
    path = Path("index.html")
    if not path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return path.read_bytes().decode("utf-8", errors="ignore")


@app.post("/connect-ig", response_model=ConnectIGResponse)
def connect_ig(payload: ConnectIGRequest, request: Request = None):
    ensure_sessions_dir()
    if is_paused(request.state.user_id):
        raise HTTPException(status_code=429, detail="Account paused for 24h due to challenge. Reconnect later.")
    if payload.ig_username == MOCK_IG_USERNAME and payload.ig_password == MOCK_IG_PASSWORD:
        session_dict = {"mock": True}
        encrypted = encrypt_session(session_dict)
        file_path = get_session_path(payload.ig_username)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(encrypted)
        request.state.ig_username = payload.ig_username
        save_last_username(request.state.user_id, payload.ig_username)
        return ConnectIGResponse(status="connected")
    client = get_instagram_client(None, payload.ig_username)
    try:
        login_with_retry(client, payload.ig_username, payload.ig_password)
    except Exception as exc:
        if isinstance(exc, ChallengeRequired):
            pause_user(request.state.user_id)
            raise HTTPException(status_code=401, detail="Challenge required. Account paused for 24h.")
        handle_ig_exception(exc)
        raise HTTPException(status_code=400, detail=f"Instagram login failed: {exc}")

    session_dict = client.get_settings()
    encrypted = encrypt_session(session_dict)
    file_path = get_session_path(payload.ig_username)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(encrypted)

    request.state.ig_username = payload.ig_username
    save_last_username(request.state.user_id, payload.ig_username)
    return ConnectIGResponse(status="connected")


@app.get("/search", response_model=SearchResponse)
@limiter.limit("3/hour")
def search(niche: str, min_f: int = 0, max_f: int = 10000000, min_e: float = 0.0, tags: str | None = None, ig_username: str | None = None, request: Request = None):
    if is_paused(request.state.user_id):
        raise HTTPException(status_code=429, detail="Account paused for 24h due to challenge. Reconnect later.")
    enforce_internal_cap(request.state.user_id)

    if niche not in HASHTAG_MAP and niche != "custom":
        raise HTTPException(status_code=400, detail="Unsupported niche")

    if not ig_username:
        ig_username = load_last_username(request.state.user_id)
    if not ig_username:
        ig_username = get_latest_session_username()
    if not ig_username:
        ig_username = request.state.ig_username
    if not ig_username:
        raise HTTPException(status_code=400, detail="Instagram not connected")

    file_path = get_session_path(ig_username)
    if not file_path.exists():
        raise HTTPException(status_code=400, detail="Instagram not connected")

    encrypted = file_path.read_text(encoding="utf-8")
    session_dict = decrypt_session(encrypted)
    client = get_instagram_client(session_dict, ig_username)

    try:
        client.get_timeline_feed()
    except Exception as exc:
        if isinstance(exc, ChallengeRequired):
            pause_user(request.state.user_id)
            raise HTTPException(status_code=401, detail="Challenge required. Account paused for 24h.")
        if isinstance(exc, LoginRequired):
            raise HTTPException(status_code=401, detail="Reconnect needed")
        handle_ig_exception(exc)
        raise HTTPException(status_code=400, detail=f"Instagram search failed: {exc}")

    results = []
    seen_user_ids = set()
    if niche == "custom":
        if not tags:
            raise HTTPException(status_code=400, detail="Custom hashtags required")
        hashtags = [t.strip().lstrip("#") for t in tags.split(",") if t.strip()]
        hashtags = hashtags[:3]
        if not hashtags:
            raise HTTPException(status_code=400, detail="Custom hashtags required")
    else:
        hashtags = HASHTAG_MAP[niche][:3]

    try:
        for tag in hashtags:
            medias = get_hashtag_medias(client, tag, amount=15)
            for media in medias:
                if len(results) >= 20:
                    break
                user_id = get_media_user_pk(media)
                if not user_id:
                    continue
                if user_id in seen_user_ids:
                    continue
                info = client.user_info(user_id)
                follower_count = int(info.follower_count or 0)
                if follower_count < min_f or follower_count > max_f:
                    continue
                engagement_rate = 0.0
                if follower_count > 0:
                    engagement_rate = (float(get_media_like_count(media) or 0) + float(get_media_comment_count(media) or 0)) / float(follower_count)
                if engagement_rate < min_e:
                    continue
                seen_user_ids.add(user_id)
                results.append(
                    SearchResult(
                        ig_username=info.username,
                        follower_count=follower_count,
                        engagement_rate=engagement_rate,
                    )
                )
            if len(results) >= 20:
                break
    except Exception as exc:
        if isinstance(exc, ChallengeRequired):
            pause_user(request.state.user_id)
            raise HTTPException(status_code=401, detail="Challenge required. Account paused for 24h.")
        handle_ig_exception(exc)
        raise HTTPException(status_code=400, detail=f"Instagram search failed: {exc}")

    if not session_dict.get("mock"):
        updated = encrypt_session(client.get_settings())
        file_path.write_text(updated, encoding="utf-8")
    return SearchResponse(results=results)
