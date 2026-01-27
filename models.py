from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    whop_id = Column(String, unique=True, index=True, nullable=False)
    ig_username = Column(String, nullable=True)
    encrypted_session = Column(Text, nullable=True)
    last_activity = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())