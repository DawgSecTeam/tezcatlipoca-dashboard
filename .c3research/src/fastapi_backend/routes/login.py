"""JWT auth endpoints."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from jose import jwt
from pydantic import BaseModel
from sqlmodel import Session, select

from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import Users
from fastapi_backend.utils.auth import (
    SECRET_KEY,
    get_current_user,
    verify_password,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    """POST /login request body."""

    username: str
    password: str


@router.post("/login")
def login_user(data: LoginRequest) -> dict[str, Any]:
    """Authenticate user credentials and return a JWT bearer token."""
    try:
        with Session(engine) as session:
            statement = select(Users).where(Users.username == data.username)
            user = session.exec(statement).first()

            # hash
            # hashed_password = hash_password(user.password)

            if not user or not verify_password(data.password, user.password):
                raise HTTPException(status_code=401, detail="Invalid credentials")

            token = jwt.encode(
                {"sub": str(user.user_id)}, SECRET_KEY, algorithm="HS256"
            )

            return {
                "token": token,
                "user_id": user.user_id,
                "username": user.username,
                "admin": user.is_admin,
                "is_blue_team": user.is_blue_team,
                "blueteam_num": user.blue_team_num,
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to add user: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/me")
def get_me(current_user: Users = Depends(get_current_user)) -> dict[str, Any]:
    """Return the authenticated user's profile."""
    return {
        "user": {
            "user_id": current_user.user_id,
            "username": current_user.username,
            "admin": current_user.is_admin,
            "is_blue_team": current_user.is_blue_team,
            "blueteam_num": current_user.blue_team_num,
        }
    }
