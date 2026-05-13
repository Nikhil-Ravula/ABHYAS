"""
Single Device Login Middleware
==============================
Ensures each user can only have ONE active session at a time.

How it works:
- On every request from an authenticated user, check if their current
  session key matches the one stored in UserSession.
- If it doesn't match, it means they logged in from another device,
  so this (older) session gets invalidated.
- For unauthenticated users, no database query is made (efficient).
"""

import logging
from django.contrib.auth import logout
from django.contrib import messages

logger = logging.getLogger(__name__)


class SingleDeviceLoginMiddleware:
    """
    Middleware that enforces single-device login.
    
    Must be placed AFTER:
    - SessionMiddleware (needs request.session)
    - AuthenticationMiddleware (needs request.user)
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # ── Only check for authenticated users ──
        # This avoids unnecessary DB queries for anonymous visitors
        if request.user.is_authenticated:
            current_session_key = request.session.session_key

            try:
                # Fetch the stored active session for this user
                user_session = request.user.user_session

                # If stored session key doesn't match current one → kick them out
                # This means they logged in from another device/browser
                if user_session.session_key and user_session.session_key != current_session_key:
                    # Notify the user why they were logged out
                    messages.warning(
                        request,
                        "You have been logged out because your account was accessed from another device."
                    )
                    # Flush the current session and log them out
                    logout(request)
                    logger.warning(f"User {request.user.username} logged out due to concurrent session from another device")

            except Exception as e:
                # No UserSession record exists for this user yet (legacy users)
                # or unexpected database error. This is fine — UserSession will be created on next login
                logger.debug(f"UserSession check error: {e}")

        response = self.get_response(request)
        return response
