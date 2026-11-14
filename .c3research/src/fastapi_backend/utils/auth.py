"""JWT issuance/validation, password hashing helpers."""

import bcrypt
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlmodel import Session, select

from fastapi_backend.core.config import config
from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import Users

SECRET_KEY = config.JWT_SECRET_KEY
ALGORITHM = config.JWT_ALGORITHM

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Constant-time bcrypt comparison."""
    return bcrypt.checkpw(
        plain_password.encode("utf-8"), hashed_password.encode("utf-8")
    )


def hash_password(plain_password: str) -> str:
    """Return the bcrypt hash of `plain_password` as a UTF-8 string."""
    hashed = bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def add_user(user: Users) -> None:
    """Persist a new user, validating uniqueness of id and username."""
    from fastapi_backend.database.write_actor import write_actor  # cycle-break

    hashed_password = hash_password(user.password)
    # Validation + insert in ONE actor transaction so the uniqueness check reads
    # from the same DB the insert will land in (avoids a read-vs-write engine
    # mismatch when tests patch only their own engine module reference).
    def _upsert(db: Session) -> str | None:
        if user.user_id != -1 and db.get(Users, user.user_id) is not None:
            return "id"
        existing_username = db.execute(
            select(Users).where(Users.username == user.username)
        ).scalar_one_or_none()
        if existing_username is not None:
            return "name"
        if user.user_id:
            new_user = Users(
                user_id=user.user_id,
                username=user.username,
                password=hashed_password,
                is_admin=user.is_admin,
                is_blue_team=user.is_blue_team,
                blue_team_num=user.blue_team_num if user.is_blue_team else None,
            )
        else:
            new_user = Users(
                username=user.username,
                password=hashed_password,
                is_admin=user.is_admin,
                is_blue_team=user.is_blue_team,
                blue_team_num=user.blue_team_num if user.is_blue_team else None,
            )
        db.add(new_user)
        return None
    err = write_actor.submit_sync(_upsert, label="add_user")
    if err == "id":
        raise HTTPException(status_code=400, detail="User ID already exists")
    if err == "name":
        raise HTTPException(status_code=400, detail="Username already exists")


def get_user_from_token(token: str) -> Users:
    """Decode the JWT and return the matching `Users` row, or raise 401."""
    try:
        # print("Raw token received:", token)
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        # print("JWT payload:", payload)

        user_id = payload.get("sub")
        # print("Extracted user_id:", user_id)

        with Session(engine) as session:
            user = session.exec(select(Users).where(Users.user_id == user_id)).first()
            # print("Found user in DB:", user)

            if user:
                return user

    except JWTError:
        pass
    except Exception:
        pass

    raise HTTPException(status_code=401, detail="Invalid token")


def get_current_user(token: str = Depends(oauth2_scheme)) -> Users:
    """FastAPI dep — extract the bearer token and resolve to a `Users` row."""
    return get_user_from_token(token)
