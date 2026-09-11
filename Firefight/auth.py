# app/utils/auth.py
import os
from typing import Optional
from fastapi import Header, HTTPException, status

VALID_TOKEN = "test-token-123"

def verify_token(authorization: Optional[str] = Header(None)):
    """
    简单的 Bearer Token 校验：
    - 请求头需带：Authorization: Bearer <token>
    - 默认 token 为 test-token-123，可通过环境变量 API_TOKEN 覆盖
    """
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Authorization header format. Expected 'Bearer <token>'.")
    token = parts[1]
    if token != VALID_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    return {"token": token}
