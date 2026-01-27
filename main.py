import json
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
    request.state.ig_username = "mvp"
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


def enforce_internal_cap(user_id: int):
    redis_client = get_redis()
    hour_key = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    key = f"cap:{user_id}:{hour_key}"
    if redis_client:
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, 3600)
        if count > 30:
            raise HTTPException(status_code=429, detail="Hourly request cap exceeded")
        return
    count = _memory_cap.get(key, 0) + 1
    _memory_cap[key] = count
    if count > 30:
        raise HTTPException(status_code=429, detail="Hourly request cap exceeded")


@app.get("/", response_class=HTMLResponse)
def index():
    path = Path("index.html")
    if not path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return path.read_bytes().decode("utf-8", errors="ignore")


@app.post("/connect-ig", response_model=ConnectIGResponse)
def connect_ig(payload: ConnectIGRequest, request: Request = None):
    ensure_sessions_dir()
    if payload.ig_username == MOCK_IG_USERNAME and payload.ig_password == MOCK_IG_PASSWORD:
        session_dict = {"mock": True}
        encrypted = encrypt_session(session_dict)
        file_path = get_session_path(payload.ig_username)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(encrypted)
        request.state.ig_username = payload.ig_username
        return ConnectIGResponse(status="connected")
    client = get_instagram_client(None)
    try:
        client.login(payload.ig_username, payload.ig_password)
    except Exception as exc:
        handle_ig_exception(exc)
        raise HTTPException(status_code=400, detail=f"Instagram login failed: {exc}")

    session_dict = client.get_settings()
    encrypted = encrypt_session(session_dict)
    file_path = get_session_path(payload.ig_username)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(encrypted)

    request.state.ig_username = payload.ig_username
    return ConnectIGResponse(status="connected")


@app.get("/search", response_model=SearchResponse)
@limiter.limit("5/hour")
def search(niche: str, min_f: int = 0, max_f: int = 10000000, min_e: float = 0.0, tags: str | None = None, request: Request = None):
    enforce_internal_cap(request.state.user_id)

    if niche not in HASHTAG_MAP and niche != "custom":
        raise HTTPException(status_code=400, detail="Unsupported niche")

    ig_username = request.state.ig_username
    if not ig_username:
        raise HTTPException(status_code=400, detail="Instagram not connected")

    file_path = get_session_path(ig_username)
    if not file_path.exists():
        raise HTTPException(status_code=400, detail="Instagram not connected")

    encrypted = file_path.read_text(encoding="utf-8")
    session_dict = decrypt_session(encrypted)
    client = get_instagram_client(session_dict)

    try:
        client.get_timeline_feed()
    except Exception as exc:
        if isinstance(exc, LoginRequired):
            raise HTTPException(status_code=401, detail="Reconnect needed")
        handle_ig_exception(exc)
        raise HTTPException(status_code=400, detail="Instagram search failed")

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
            medias = client.hashtag_medias_recent(tag, amount=15)
            for media in medias:
                if len(results) >= 20:
                    break
                user_id = media.user.pk
                if user_id in seen_user_ids:
                    continue
                info = client.user_info(user_id)
                follower_count = int(info.follower_count or 0)
                if follower_count < min_f or follower_count > max_f:
                    continue
                engagement_rate = 0.0
                if follower_count > 0:
                    engagement_rate = (float(media.like_count or 0) + float(media.comment_count or 0)) / float(follower_count)
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
        handle_ig_exception(exc)
        raise HTTPException(status_code=400, detail="Instagram search failed")

    return SearchResponse(results=results)
