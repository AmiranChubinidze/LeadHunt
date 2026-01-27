from sqlalchemy.orm import Session

from models import User


def get_user_by_whop_id(db: Session, whop_id: str):
    return db.query(User).filter(User.whop_id == whop_id).first()


def get_user_by_id(db: Session, user_id: int):
    return db.query(User).filter(User.id == user_id).first()


def create_user(db: Session, whop_id: str, ig_username: str | None, encrypted_session: str | None):
    user = User(whop_id=whop_id, ig_username=ig_username, encrypted_session=encrypted_session)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def update_user_session(db: Session, user: User, ig_username: str, encrypted_session: str):
    user.ig_username = ig_username
    user.encrypted_session = encrypted_session
    db.add(user)
    db.commit()
    db.refresh(user)
    return user