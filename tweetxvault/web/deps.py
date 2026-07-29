"""Shared dependencies and server state for tweetxvault web server."""

import secrets
import hashlib
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

server_state: dict[str, Any] = {}
security = HTTPBasic()

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)) -> bool:
    expected_hash = server_state.get("password_hash")
    if not expected_hash:
        return True
        
    input_hash = hashlib.sha256(credentials.password.encode("utf8")).hexdigest()
    is_password_correct = secrets.compare_digest(input_hash, expected_hash)
    
    if not is_password_correct:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True

def get_store():
    return server_state.get("store")

def get_server_state():
    return server_state
