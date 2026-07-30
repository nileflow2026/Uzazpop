"""
routers/auth.py
───────────────
Authentication endpoints: login, token refresh, password change.

ENDPOINTS:
  POST /auth/login          → returns access + refresh token pair
  POST /auth/refresh        → exchange refresh token for new access token
  POST /auth/change-password → change own password (authenticated)
  GET  /auth/me             → return current user profile

SECURITY NOTES:
  • Failed logins return a generic message (no "email not found" vs "wrong password"
    distinction) to prevent user enumeration.
  • last_login timestamp updated on every successful login for audit.
  • Password change requires current password verification.
"""

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from database import get_db
from models.orm import AuditLog, MpesaStatus, MpesaTransaction, PasswordResetCode, User
from schemas.schemas import (
    ForgotPasswordIn, LoginIn, PasswordChangeIn, RegisterInitiateIn, RegisterInitiateOut,
    ResetPasswordIn, TokenOut, TokenRefreshIn, UserOut,
)
from services.mpesa_service import stk_push
from utils.email import send_password_reset_code
from utils.security import (
    create_access_token, create_refresh_token, decode_refresh_token,
    get_current_user, hash_password, verify_password,
    ACCESS_TOKEN_EXPIRE_MINUTES,
)

from slowapi import Limiter
from slowapi.util import get_remote_address
from fastapi import Request

limiter = Limiter(key_func=get_remote_address)

from config import get_settings

settings = get_settings()
router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/login", response_model=TokenOut, summary="Login with email + password")
@limiter.limit("5/minute")
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)):
    """
    Authenticate a staff member.  Returns JWT access + refresh tokens.

    The access token is short-lived (30 min default).
    The refresh token is long-lived (7 days default) and used only to
    obtain new access tokens – never for data requests.
    """
    # Generic error for both "not found" and "wrong password" – prevents enumeration
    invalid_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password",
        headers={"WWW-Authenticate": "Bearer"},
    )

    user: User = db.query(User).filter(User.email == payload.email.lower()).first()
    if not user:
        raise invalid_credentials

    if not verify_password(payload.password, user.hashed_password):
        # Log failed attempt for monitoring
        db.add(AuditLog(
            user_id=None,
            action="LOGIN_FAILED",
            detail=f"Failed login attempt for email={payload.email}",
            ip_address=request.client.host if request.client else None,
        ))
        db.commit()
        raise invalid_credentials

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled. Contact your administrator.",
        )

    # Update last_login timestamp
    user.last_login = datetime.now(timezone.utc)

    # Audit successful login
    db.add(AuditLog(
        user_id=user.id,
        action="LOGIN_SUCCESS",
        detail=f"User {user.email} logged in",
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()

    return TokenOut(
        access_token=create_access_token(user.id, user.role.value),
        refresh_token=create_refresh_token(user.id),
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/refresh", response_model=TokenOut, summary="Refresh access token")
def refresh_token(payload: TokenRefreshIn, db: Session = Depends(get_db)):
    """
    Exchange a valid refresh token for a new access token + refresh token pair.
    Rotating refresh tokens limits the window of token theft exploitation.
    """
    user_id = decode_refresh_token(payload.refresh_token)
    user = db.get(User, user_id)

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or account disabled",
        )

    return TokenOut(
        access_token=create_access_token(user.id, user.role.value),
        refresh_token=create_refresh_token(user.id),   # Rotate refresh token
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.get("/me", response_model=UserOut, summary="Get current user profile")
def me(current_user: User = Depends(get_current_user)):
    """Return the authenticated user's profile."""
    return current_user


@router.post("/change-password", summary="Change own password")
def change_password(
    payload: PasswordChangeIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Allow a user to change their own password.
    Requires correct current password to prevent session-hijack exploitation.
    """
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    if payload.new_password == payload.current_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must differ from current password",
        )

    current_user.hashed_password = hash_password(payload.new_password)

    db.add(AuditLog(
        user_id=current_user.id,
        action="PASSWORD_CHANGED",
        entity="User",
        entity_id=current_user.id,
        detail="User changed their own password",
    ))
    db.commit()

    return {"message": "Password changed successfully"}


@router.post("/forgot-password", summary="Request a password reset code by email")
@limiter.limit("3/minute")
def forgot_password(payload: ForgotPasswordIn, request: Request, db: Session = Depends(get_db)):
    """
    If the email belongs to an active account, emails a 6-digit reset code
    (valid for PASSWORD_RESET_CODE_EXPIRE_MINUTES). Always returns the same
    generic message regardless of whether the email exists, to prevent
    user enumeration — same principle as the login endpoint.
    """
    generic_response = {
        "message": "If that email is registered, a reset code has been sent to it."
    }

    user = db.query(User).filter(User.email == payload.email.lower()).first()
    if not user or not user.is_active:
        return generic_response  # Don't reveal whether the account exists

    # Invalidate any previous unused codes for this user
    db.query(PasswordResetCode).filter(
        PasswordResetCode.user_id == user.id,
        PasswordResetCode.used == False,  # noqa: E712
    ).update({"used": True})

    code = f"{secrets.randbelow(1_000_000):06d}"
    reset_row = PasswordResetCode(
        user_id=user.id,
        code_hash=hash_password(code),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.PASSWORD_RESET_CODE_EXPIRE_MINUTES),
    )
    db.add(reset_row)
    db.add(AuditLog(
        user_id=user.id,
        action="PASSWORD_RESET_REQUESTED",
        detail=f"Password reset code requested for {user.email}",
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()

    send_password_reset_code(
        to_email=user.email,
        full_name=user.full_name,
        code=code,
        expire_minutes=settings.PASSWORD_RESET_CODE_EXPIRE_MINUTES,
    )

    return generic_response


@router.post("/reset-password", summary="Reset password using an emailed code")
@limiter.limit("5/minute")
def reset_password(payload: ResetPasswordIn, request: Request, db: Session = Depends(get_db)):
    """
    Verify a 6-digit reset code (from /auth/forgot-password) and set a
    new password. Codes are single-use, expire after
    PASSWORD_RESET_CODE_EXPIRE_MINUTES, and are locked after 5 wrong
    attempts to resist brute-forcing the 6-digit space.
    """
    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid or expired reset code",
    )

    user = db.query(User).filter(User.email == payload.email.lower()).first()
    if not user:
        raise invalid

    reset_row = (
        db.query(PasswordResetCode)
        .filter(
            PasswordResetCode.user_id == user.id,
            PasswordResetCode.used == False,  # noqa: E712
        )
        .order_by(PasswordResetCode.created_at.desc())
        .first()
    )
    if not reset_row:
        raise invalid

    if reset_row.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise invalid

    if reset_row.attempts >= 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Too many incorrect attempts. Request a new reset code.",
        )

    if not verify_password(payload.code, reset_row.code_hash):
        reset_row.attempts += 1
        db.commit()
        raise invalid

    # Success — apply new password, burn the code
    user.hashed_password = hash_password(payload.new_password)
    reset_row.used = True

    db.add(AuditLog(
        user_id=user.id,
        action="PASSWORD_RESET_COMPLETED",
        detail=f"Password reset via emailed code for {user.email}",
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()

    return {"message": "Password reset successfully. You can now sign in."}


@router.get("/registration-fee", summary="Return the registration fee")
def get_registration_fee():
    """Returns the registration fee in KES. Always KES 300."""
    return {
        "amount": settings.REGISTRATION_FEE_KES,
        "currency": "KES",
        "label": f"KES {settings.REGISTRATION_FEE_KES}",
        "test_mode": False,
    }


@router.post("/register/initiate", response_model=RegisterInitiateOut,
             summary="Start paid self-registration for a new pharmacy install (bootstrap only)")
@limiter.limit("3/minute")
def register_initiate(payload: RegisterInitiateIn, request: Request, db: Session = Depends(get_db)):
    """
    Public endpoint used by the landing page's signup flow.

    This system is one deployment per pharmacy — this endpoint only works
    on a FRESH install that has no users yet. It sends a real M-Pesa STK
    Push for the registration fee; the admin account is only created once
    that payment is confirmed by the M-Pesa callback (see
    routers/payments.py::mpesa_callback). Nothing is created here yet —
    this just starts the payment and stashes the pending account details
    on the MpesaTransaction row until payment succeeds.

    Poll GET /payments/mpesa/{checkout_request_id}/status to know when
    it's done, matching the existing sales-payment flow.
    """
    email = payload.email.strip().lower()
    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    if "@" not in email:
        raise HTTPException(status_code=400, detail="Enter a valid email address")

    try:
        amount_kes = settings.REGISTRATION_FEE_KES
        result = stk_push(
            phone_number=payload.phone_number,
            amount=amount_kes,
            account_reference="SIGNUP",
            description="Uza Pap signup",
        )

        txn = MpesaTransaction(
            sale_id=None,
            purpose="registration",
            checkout_request_id=result["CheckoutRequestID"],
            merchant_request_id=result["MerchantRequestID"],
            phone_number=payload.phone_number,
            amount=amount_kes,
            status=MpesaStatus.PENDING,
            pending_full_name=payload.full_name.strip(),
            pending_pharmacy_name=payload.pharmacy_name.strip(),
            pending_email=email,
            pending_password_hash=hash_password(payload.password),
            pending_phone=payload.phone_number,
        )
        db.add(txn)
        db.add(AuditLog(
            user_id=None,
            action="REGISTRATION_PAYMENT_INITIATED",
            detail=f"Self-registration STK push sent to {payload.phone_number} for {email}",
            ip_address=request.client.host if request.client else None,
        ))
        db.commit()
        db.refresh(txn)

        return RegisterInitiateOut(
            checkout_request_id=txn.checkout_request_id,
            amount=amount_kes,
            message="Payment prompt sent. Ask the customer to enter their M-Pesa PIN.",
        )

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=502, detail="Could not initiate M-Pesa payment. Please try again.")
