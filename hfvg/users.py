"""User identity and authentication system.

Lightweight per-user identity with role-based access:
- users table: name, email, role (admin/member), created_at, last_active
- password/token-based auth (bcrypt hashed)
- user_id tagging on episodes, jobs, ledger rows
- legacy/shared user for existing data

Caddy basic auth remains as the outer layer (unchanged).
"""

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import bcrypt

from hfvg.config import config


# Special user IDs
LEGACY_USER_ID = "legacy/shared"
SYSTEM_USER_ID = "system"

# Roles
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
VALID_ROLES = (ROLE_ADMIN, ROLE_MEMBER)


class UserManager:
    """Manage users, authentication, and permissions."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.DB_PATH

    async def init_db(self):
        """Initialize users table."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    role TEXT NOT NULL,
                    password_hash TEXT,
                    api_token TEXT UNIQUE,
                    monthly_cap_usd_micros INTEGER,
                    created_at TEXT NOT NULL,
                    last_active TEXT,
                    disabled INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_users_api_token ON users(api_token)
            """)
            
            # User activity log
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_activity_user_timestamp 
                ON user_activity(user_id, timestamp)
            """)
            
            await db.commit()
            
            # Ensure legacy/shared user exists
            await self._ensure_legacy_user(db)

    async def _ensure_legacy_user(self, db):
        """Ensure legacy/shared user exists for backward compatibility."""
        async with db.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (LEGACY_USER_ID,)
        ) as cursor:
            if await cursor.fetchone():
                return
        
        await db.execute("""
            INSERT INTO users (user_id, name, email, role, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (LEGACY_USER_ID, "Legacy/Shared", "legacy@system", ROLE_ADMIN, 
              datetime.now(timezone.utc).isoformat()))
        await db.commit()

    def _generate_user_id(self, email: str) -> str:
        """Generate a user_id from email."""
        return f"usr_{hashlib.sha256(email.encode()).hexdigest()[:12]}"

    def _generate_api_token(self) -> str:
        """Generate a secure API token."""
        return f"hfvg_{secrets.token_urlsafe(32)}"

    def _hash_password(self, password: str) -> str:
        """Hash password with bcrypt."""
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    def _verify_password(self, password: str, hash_str: str) -> bool:
        """Verify password against hash."""
        try:
            return bcrypt.checkpw(password.encode(), hash_str.encode())
        except Exception:
            return False

    async def create_user(
        self,
        name: str,
        email: str,
        role: str = ROLE_MEMBER,
        password: Optional[str] = None,
        monthly_cap_usd_micros: Optional[int] = None,
    ) -> dict:
        """
        Create a new user.
        
        Args:
            name: User's full name
            email: User's email (must be unique)
            role: admin or member
            password: Optional password (if None, uses API token only)
            monthly_cap_usd_micros: Optional per-user monthly spending cap
            
        Returns:
            User dict with credentials (api_token shown only on creation)
        """
        if role not in VALID_ROLES:
            raise ValueError(f"role must be one of {VALID_ROLES}")
        
        email = email.lower().strip()
        user_id = self._generate_user_id(email)
        raw_api_token = self._generate_api_token()
        # Hash the API token with SHA256
        api_token_hash = hashlib.sha256(raw_api_token.encode()).hexdigest()
        password_hash = self._hash_password(password) if password else None
        
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            try:
                await db.execute("""
                    INSERT INTO users 
                    (user_id, name, email, role, password_hash, api_token, 
                     monthly_cap_usd_micros, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (user_id, name, email, role, password_hash, api_token_hash,
                      monthly_cap_usd_micros, datetime.now(timezone.utc).isoformat()))
                await db.commit()
            except aiosqlite.IntegrityError as e:
                if "email" in str(e).lower():
                    raise ValueError(f"User with email {email} already exists")
                raise
        
        return {
            "user_id": user_id,
            "name": name,
            "email": email,
            "role": role,
            "api_token": raw_api_token,  # Only shown on creation (unhashed)
            "monthly_cap_usd_micros": monthly_cap_usd_micros,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    async def authenticate_token(self, api_token: str) -> Optional[dict]:
        """Authenticate user by API token (hashed with SHA256, constant-time compare)."""
        if not api_token or not api_token.startswith("hfvg_"):
            return None
        
        # Hash the provided token
        token_hash = hashlib.sha256(api_token.encode()).hexdigest()
        
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT user_id, name, email, role, monthly_cap_usd_micros, disabled, api_token
                FROM users WHERE api_token = ?
            """, (token_hash,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                
                if row["disabled"]:
                    return None
                
                # Constant-time compare of hashes
                if not secrets.compare_digest(token_hash, row["api_token"]):
                    return None
                
                # Update last_active
                await db.execute(
                    "UPDATE users SET last_active = ? WHERE user_id = ?",
                    (datetime.now(timezone.utc).isoformat(), row["user_id"])
                )
                await db.commit()
                
                user_dict = dict(row)
                user_dict.pop("api_token", None)  # Never return hash
                return user_dict

    async def authenticate_password(self, email: str, password: str) -> Optional[dict]:
        """Authenticate user by email and password."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT user_id, name, email, role, password_hash, 
                       monthly_cap_usd_micros, disabled
                FROM users WHERE email = ?
            """, (email.lower(),)) as cursor:
                row = await cursor.fetchone()
                if not row or not row["password_hash"]:
                    return None
                
                if row["disabled"]:
                    return None
                
                if not self._verify_password(password, row["password_hash"]):
                    return None
                
                # Update last_active
                await db.execute(
                    "UPDATE users SET last_active = ? WHERE user_id = ?",
                    (datetime.now(timezone.utc).isoformat(), row["user_id"])
                )
                await db.commit()
                
                user_dict = dict(row)
                user_dict.pop("password_hash", None)  # Never return hash
                return user_dict

    async def get_user(self, user_id: str) -> Optional[dict]:
        """Get user by user_id."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT user_id, name, email, role, monthly_cap_usd_micros,
                       created_at, last_active, disabled
                FROM users WHERE user_id = ?
            """, (user_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def list_users(self, include_disabled: bool = False) -> list[dict]:
        """List all users."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            db.row_factory = aiosqlite.Row
            query = """
                SELECT user_id, name, email, role, monthly_cap_usd_micros,
                       created_at, last_active, disabled
                FROM users
            """
            if not include_disabled:
                query += " WHERE disabled = 0"
            query += " ORDER BY created_at DESC"
            
            async with db.execute(query) as cursor:
                return [dict(row) async for row in cursor]

    async def update_user_cap(self, user_id: str, monthly_cap_usd_micros: Optional[int]):
        """Update user's monthly spending cap."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute("""
                UPDATE users SET monthly_cap_usd_micros = ? WHERE user_id = ?
            """, (monthly_cap_usd_micros, user_id))
            await db.commit()

    async def disable_user(self, user_id: str):
        """Disable a user (soft delete)."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute(
                "UPDATE users SET disabled = 1 WHERE user_id = ?", (user_id,)
            )
            await db.commit()

    async def log_activity(self, user_id: str, action: str, details: Optional[str] = None):
        """Log user activity."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute("""
                INSERT INTO user_activity (user_id, action, details, timestamp)
                VALUES (?, ?, ?, ?)
            """, (user_id, action, details, datetime.now(timezone.utc).isoformat()))
            await db.commit()
