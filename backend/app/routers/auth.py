from fastapi import APIRouter, HTTPException, Response, Request, status, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
import secrets
import logging
import os
from typing import Optional

router = APIRouter()
logger = logging.getLogger(__name__)

# Credentials from environment
DASHBOARD_USER = os.getenv("DASHBOARD_USER")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
SESSION_COOKIE_NAME = "auth_session"
SESSION_TTL_DAYS = 7
SESSION_TTL_SECONDS = SESSION_TTL_DAYS * 24 * 60 * 60

class LoginRequest(BaseModel):
    username: str
    password: str

def get_redis_manager(request: Request):
    # Depending on how app structure is set up, better to get from app state or global import
    # Assuming main.py puts redis_manager in app.state or similar, but simpler is to import 
    # if circular imports serve.
    # For now, let's assume we can access it via request.app.state if available, 
    # but based on main.py review, redis_manager is a global in main.
    # To avoid circular import issues, we'll access it dynamically if possible, or import if safe.
    # Looking at main.py: redis_manager is at module level.
    # It's better to use Dependency Injection or import. 
    # Let's try importing from app.main, but wait, app.main imports routers... circular.
    # We will modify main.py to pass redis_manager or import it from a common place if needed.
    # Actually, in main.py `redis_manager` is initialized. 
    # We can attach it to app.state.redis = redis_manager in main.py startup, 
    # and access it here via request.app.state.redis
    if hasattr(request.app, 'state') and hasattr(request.app.state, 'redis'):
        return request.app.state.redis
    raise HTTPException(status_code=500, detail="Redis manager not available")

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Serve the login page HTML."""
    # Check if already logged in
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        redis = get_redis_manager(request)
        if await redis.get_session(session_token):
            return RedirectResponse(url="/", status_code=302)

    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>NTD Trader - Login</title>
        <style>
            :root {
                --background: #09090b;
                --card: #18181b;
                --border: #27272a;
                --primary: #06b6d4;
                --primary-hover: #0891b2;
                --text: #e2e8f0;
                --text-muted: #94a3b8;
                --error: #ef4444;
            }
            body {
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                background-color: var(--background);
                color: var(--text);
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
            }
            .login-container {
                background-color: var(--card);
                padding: 2.5rem;
                border-radius: 1rem;
                border: 1px solid var(--border);
                box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
                width: 100%;
                max-width: 360px;
                text-align: center;
            }
            h2 { 
                margin: 0 0 0.5rem 0; 
                color: #fff; 
                font-weight: 600;
                font-size: 1.5rem;
            }
            .subtitle {
                color: var(--text-muted);
                font-size: 0.875rem;
                margin-bottom: 2rem;
            }
            .icon-wrapper {
                width: 48px;
                height: 48px;
                background: linear-gradient(135deg, #06b6d4, #3b82f6);
                border-radius: 12px;
                display: flex;
                align-items: center;
                justify-content: center;
                margin: 0 auto 1.5rem auto;
            }
            .icon-wrapper svg {
                color: white;
                width: 24px;
                height: 24px;
            }
            .form-group { margin-bottom: 1.25rem; text-align: left; }
            label { 
                display: block; 
                margin-bottom: 0.5rem; 
                font-size: 0.875rem; 
                font-weight: 500; 
                color: var(--text-muted);
            }
            input {
                width: 100%;
                padding: 0.75rem;
                border: 1px solid var(--border);
                border-radius: 0.5rem;
                background-color: #000000;
                color: #fff;
                box-sizing: border-box;
                font-size: 0.95rem;
                transition: border-color 0.2s, box-shadow 0.2s;
            }
            input:focus { 
                outline: none; 
                border-color: var(--primary);
                box-shadow: 0 0 0 2px rgba(6, 182, 212, 0.2);
            }
            button {
                width: 100%;
                padding: 0.75rem;
                background: linear-gradient(to right, #06b6d4, #3b82f6);
                color: white;
                border: none;
                border-radius: 0.5rem;
                font-size: 0.95rem;
                font-weight: 600;
                cursor: pointer;
                transition: opacity 0.2s;
                margin-top: 0.5rem;
            }
            button:hover { opacity: 0.9; }
            .error { 
                background-color: rgba(239, 68, 68, 0.1);
                color: var(--error);
                padding: 0.75rem;
                border-radius: 0.5rem;
                font-size: 0.875rem;
                margin-bottom: 1.5rem;
                display: none;
                border: 1px solid rgba(239, 68, 68, 0.2);
            }
        </style>
    </head>
    <body>
        <div class="login-container">
            <div class="icon-wrapper">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
            </div>
            <h2>NTD Trader</h2>
            <p class="subtitle">Secure Dashboard Access</p>
            
            <div id="errorMsg" class="error">Invalid credentials</div>

            <form id="loginForm">
                <div class="form-group">
                    <label for="username">Username</label>
                    <input type="text" id="username" name="username" required autocomplete="username">
                </div>
                <div class="form-group">
                    <label for="password">Password</label>
                    <input type="password" id="password" name="password" required autocomplete="current-password">
                </div>
                <button type="submit">Log In</button>
            </form>
        </div>
        <script>
            document.getElementById('loginForm').addEventListener('submit', async (e) => {
                e.preventDefault();
                const formData = new FormData(e.target);
                const data = Object.fromEntries(formData.entries());
                const errorMsg = document.getElementById('errorMsg');
                const btn = e.target.querySelector('button');
                
                // Clear previous errors
                errorMsg.style.display = 'none';
                
                btn.disabled = true;
                const originalBtnText = btn.textContent;
                btn.textContent = 'Authenticating...';

                try {
                    const response = await fetch('/login', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(data)
                    });

                    if (response.ok) {
                        window.location.href = '/';
                    } else {
                        const resData = await response.json();
                        errorMsg.textContent = resData.detail || 'Invalid credentials';
                        errorMsg.style.display = 'block';
                        btn.disabled = false;
                        btn.textContent = originalBtnText;
                    }
                } catch (err) {
                    errorMsg.textContent = 'Connection error. Please try again.';
                    errorMsg.style.display = 'block';
                    btn.disabled = false;
                    btn.textContent = originalBtnText;
                }
            });
        </script>
    </body>
    </html>
    """

@router.post("/login")
async def login(creds: LoginRequest, request: Request, response: Response):
    # Debug logging to investigate auth failure
    if not DASHBOARD_USER or not DASHBOARD_PASSWORD:
        logger.error("DASHBOARD_USER or DASHBOARD_PASSWORD not set in env")
        raise HTTPException(status_code=500, detail="Auth not configured on server")
    
    # Check simple equality
    user_match = creds.username == DASHBOARD_USER
    pass_match = creds.password == DASHBOARD_PASSWORD
    
    if not user_match or not pass_match:
        logger.warning(f"Login failed for user '{creds.username}'. Match: User={user_match}, Pass={pass_match}")
        # Be vague to user, specific in logs
        raise HTTPException(status_code=401, detail="Invalid username or password")

    if creds.username == DASHBOARD_USER and creds.password == DASHBOARD_PASSWORD:
        token = secrets.token_urlsafe(32)
        redis = get_redis_manager(request)
        
        # Store session
        user_data = {"username": creds.username}
        await redis.set_session(token, user_data, SESSION_TTL_DAYS)
        
        # Set cookie
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=False # Set to True if HTTPS is enabled, but we are running behind nginx proxy which usually handles https or is internal.
            # Given current setup seems to be http for local/internal, secure=False is safer to start.
            # If nginx terminates TLS, we can set secure=True if we trust the loopback/proxy headers.
            # For now, lax/false is robust for this specific setup without TLS configured in the prompt context.
        )
        return {"success": True}
    
    raise HTTPException(status_code=401, detail="Invalid credentials")

@router.post("/logout")
async def logout(request: Request, response: Response):
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        redis = get_redis_manager(request)
        await redis.delete_session(session_token)
    
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"success": True}

@router.get("/auth/validate")
async def validate_session(request: Request):
    """
    Internal endpoint for nginx auth_request.
    Returns 200 if authenticated, 401 otherwise.
    """
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        raise HTTPException(status_code=401, detail="No session")
    
    redis = get_redis_manager(request)
    session = await redis.get_session(session_token)
    
    if session:
        # Extend session? Optionally here.
        # For now just validate.
        return {"status": "valid", "user": session.get("username")}
    
    raise HTTPException(status_code=401, detail="Invalid session")
