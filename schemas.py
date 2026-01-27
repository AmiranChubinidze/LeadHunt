from typing import List, Optional

from pydantic import BaseModel


class ConnectIGRequest(BaseModel):
    ig_username: str
    ig_password: str


class ConnectIGResponse(BaseModel):
    status: str


class SearchResult(BaseModel):
    ig_username: str
    follower_count: int
    engagement_rate: float


class SearchResponse(BaseModel):
    results: List[SearchResult]


class UserCreate(BaseModel):
    whop_id: str
    ig_username: Optional[str] = None
    encrypted_session: Optional[str] = None


class UserOut(BaseModel):
    id: int
    whop_id: str
    ig_username: Optional[str] = None

    class Config:
        orm_mode = True