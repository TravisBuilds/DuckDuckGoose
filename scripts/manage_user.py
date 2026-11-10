#!/usr/bin/env python3
"""
CLI for user management.

Usage:
    # Create a user (password from stdin or env)
    python scripts/manage_user.py create --name "Alice" --email alice@example.com --role admin
    echo "password123" | python scripts/manage_user.py create --name "Alice" --email alice@example.com --role admin
    USER_PASSWORD="password123" python scripts/manage_user.py create --name "Alice" --email alice@example.com --role admin
    
    # Reset a user's password
    python scripts/manage_user.py reset-password --email alice@example.com
    echo "newpassword" | python scripts/manage_user.py reset-password --email alice@example.com
    
    # List users
    python scripts/manage_user.py list
    
    # Get user details
    python scripts/manage_user.py get --email alice@example.com
"""

import asyncio
import os
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hfvg.users import UserManager, ROLE_ADMIN, ROLE_MEMBER, VALID_ROLES


def read_password_secure() -> str:
    """Read password from stdin (piped), env var, or fail."""
    # Try environment variable first
    password = os.getenv("USER_PASSWORD", "")
    if password:
        return password
    
    # Try stdin if it's piped (not a tty)
    if not sys.stdin.isatty():
        password = sys.stdin.read().strip()
        if password:
            return password
    
    # Fail - never prompt interactively to avoid password appearing in terminal
    raise ValueError(
        "Password required. Provide via stdin pipe (echo 'pass' | ...) "
        "or USER_PASSWORD environment variable. Never type passwords directly."
    )


async def create_user(args):
    """Create a new user."""
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    user_mgr = UserManager(db_path)
    await user_mgr.init_db()
    
    # Get password if not using invite mode
    password = None
    if not args.invite:
        try:
            password = read_password_secure()
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    
    try:
        user = await user_mgr.create_user(
            name=args.name,
            email=args.email,
            role=args.role,
            password=password
        )
        
        if args.invite:
            # Generate invite link
            token = await user_mgr.create_password_reset_token(user['user_id'])
            base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:3000")
            invite_url = f"{base_url}/set-password?token={token}"
            
            # Print ONLY the invite URL (no user details, no API token)
            print(invite_url)
        else:
            # Non-invite mode: show full details including API token
            print(f"✓ User created successfully")
            print(f"  User ID: {user['user_id']}")
            print(f"  Name: {user['name']}")
            print(f"  Email: {user['email']}")
            print(f"  Role: {user['role']}")
            print(f"  API Token: {user['api_token']}")
            print()
            print("⚠️  Save the API token - it will not be shown again.")
        
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


async def reset_password(args):
    """Reset a user's password."""
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    user_mgr = UserManager(db_path)
    await user_mgr.init_db()
    
    if args.invite:
        # Generate a reset link instead of setting password directly
        import aiosqlite
        
        # Find user by email
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id FROM users WHERE email = ?", (args.email.lower(),)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    print(f"Error: User with email {args.email} not found", file=sys.stderr)
                    sys.exit(1)
                user_id = row["user_id"]
        
        # Generate reset token
        token = await user_mgr.create_password_reset_token(user_id)
        base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:3000")
        reset_url = f"{base_url}/set-password?token={token}"
        
        print(f"✓ Password reset link generated for {args.email}")
        print()
        print("🔗 Reset Link (valid for 72 hours):")
        print(f"   {reset_url}")
        print()
        print("   Share this link with the user to reset their password.")
    else:
        # Direct password reset (original behavior)
        try:
            password = read_password_secure()
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        
        success = await user_mgr.set_password(args.email, password)
        
        if success:
            print(f"✓ Password reset successfully for {args.email}")
        else:
            print(f"Error: User with email {args.email} not found", file=sys.stderr)
            sys.exit(1)


async def list_users(args):
    """List all users."""
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    user_mgr = UserManager(db_path)
    await user_mgr.init_db()
    
    users = await user_mgr.list_users(include_disabled=args.include_disabled)
    
    if not users:
        print("No users found.")
        return
    
    print(f"{'User ID':<20} {'Name':<25} {'Email':<35} {'Role':<10} {'Disabled':<10}")
    print("-" * 105)
    
    for user in users:
        disabled = "Yes" if user.get("disabled") else "No"
        print(f"{user['user_id']:<20} {user['name']:<25} {user['email']:<35} {user['role']:<10} {disabled:<10}")
    
    print()
    print(f"Total: {len(users)} users")


async def get_user(args):
    """Get user details."""
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    user_mgr = UserManager(db_path)
    await user_mgr.init_db()
    
    # Find user by email
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id FROM users WHERE email = ?", (args.email.lower(),)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                print(f"Error: User with email {args.email} not found", file=sys.stderr)
                sys.exit(1)
            user_id = row["user_id"]
    
    user = await user_mgr.get_user(user_id)
    
    if not user:
        print(f"Error: User not found", file=sys.stderr)
        sys.exit(1)
    
    print(f"User Details:")
    print(f"  User ID: {user['user_id']}")
    print(f"  Name: {user['name']}")
    print(f"  Email: {user['email']}")
    print(f"  Role: {user['role']}")
    print(f"  Created: {user['created_at']}")
    print(f"  Last Active: {user.get('last_active', 'Never')}")
    print(f"  Disabled: {'Yes' if user.get('disabled') else 'No'}")
    
    if user.get('monthly_cap_usd_micros'):
        print(f"  Monthly Cap: ${user['monthly_cap_usd_micros'] / 1_000_000:.2f}")


def main():
    parser = argparse.ArgumentParser(
        description="User management CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Create user
    create_parser = subparsers.add_parser("create", help="Create a new user")
    create_parser.add_argument("--name", required=True, help="User's full name")
    create_parser.add_argument("--email", required=True, help="User's email")
    create_parser.add_argument(
        "--role", 
        default=ROLE_MEMBER, 
        choices=VALID_ROLES,
        help=f"User role (default: {ROLE_MEMBER})"
    )
    create_parser.add_argument(
        "--invite",
        action="store_true",
        help="Generate invite link instead of setting password now (user sets their own)"
    )
    
    # Reset password
    reset_parser = subparsers.add_parser("reset-password", help="Reset a user's password")
    reset_parser.add_argument("--email", required=True, help="User's email")
    reset_parser.add_argument(
        "--invite",
        action="store_true",
        help="Generate reset link instead of setting password directly"
    )
    
    # List users
    list_parser = subparsers.add_parser("list", help="List all users")
    list_parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Include disabled users"
    )
    
    # Get user
    get_parser = subparsers.add_parser("get", help="Get user details")
    get_parser.add_argument("--email", required=True, help="User's email")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    if args.command == "create":
        asyncio.run(create_user(args))
    elif args.command == "reset-password":
        asyncio.run(reset_password(args))
    elif args.command == "list":
        asyncio.run(list_users(args))
    elif args.command == "get":
        asyncio.run(get_user(args))


if __name__ == "__main__":
    main()
