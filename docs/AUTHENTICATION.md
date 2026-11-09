# Authentication & User Management

The DuckDuckGoose studio supports personal user accounts with role-based access control.

## Overview

Three authentication methods are available:

1. **Admin Secret** (legacy): Shared secret for initial setup and automated scripts
2. **Email + Password**: Personal accounts with role-based access control
3. **API Token**: For programmatic access (production integrations, see PRODUCTION-API.md)

## User Roles

### Admin Role
Full access to all studio features:
- All episode management (start, upload, approve/reject, retry, status, downloads)
- User management (create, list, reset passwords, manage roles)
- Spending dashboard (balance, costs, budget tracking)
- Live mode toggles and budget cap configuration

### Member Role
Episode management access only:
- Start new episodes (`POST /api/episodes`)
- Upload beatmaps/continuity (`POST /api/episodes/upload`, `POST /api/episodes/upload/validate`)
- View episodes and shots (`GET /api/episodes`, `GET /api/episodes/{id}`)
- Approve/reject stills and clips (`POST /api/episodes/{id}/shots/{shot}/approve`, `/reject`)
- Retry failed shots (`POST /api/episodes/{id}/shots/{shot}/retry-still`, `/retry-clip`)
- Story shape editing and approval (`GET /api/episodes/{id}/story-shape`, `POST .../approve`)
- Final cut approval/rejection (`POST /api/episodes/{id}/final-cut/approve`, `/reject`)
- View audit trails (`GET /api/episodes/{id}/audit`)
- Download episode assets (`GET /api/episodes/{id}/download`)
- Toggle live mode for episodes (`POST /api/episodes/{id}/set-live`)

**Member restrictions** (403 Forbidden):
- Cannot access spending dashboard (`GET /api/dashboard`)
- Cannot view user list (`GET /api/users`)
- Cannot create users (`POST /api/users`)
- Cannot change user roles or delete users
- Cannot view or change budget caps (read-only access to episode settings)

**Design decision**: Members can run episodes in live mode (if `DRY_RUN=false` globally) but cannot change budget caps or view/modify the manual balance. This allows team members to execute production without financial oversight access.

## User Management

### Creating Users (Admin-Only)

#### Web UI (Recommended)

1. Log in as an admin
2. Navigate to the **Users** page from the studio header
3. Click **Create User**
4. Enter name, email, and role
5. Leave "Generate invite link" checked (recommended)
6. Click **Create User**
7. Copy the invite link and send it to the new user

The invite link is valid for **72 hours** and allows the user to set their own password.

#### CLI (SSH Access Required)

Create a user with an invite link:
```bash
docker compose exec api python3 scripts/manage_user.py create \
  --name "User Name" \
  --email user@example.com \
  --role member \
  --invite
```

Create a user with a direct password (not recommended - better to use invites):
```bash
echo "securepassword" | docker compose exec -T api python3 scripts/manage_user.py create \
  --name "User Name" \
  --email user@example.com \
  --role member
```

Roles: `admin` or `member`

#### API (Admin-Only)

```bash
# With invite link
curl -X POST http://localhost:8000/api/users \
  -H "Content-Type: application/json" \
  --cookie "studio_admin_token=YOUR_ADMIN_SESSION_TOKEN" \
  -d '{
    "name": "User Name",
    "email": "user@example.com",
    "role": "member"
  }'
# Response includes "invite_url" field

# With direct password
curl -X POST http://localhost:8000/api/users \
  -H "Content-Type: application/json" \
  --cookie "studio_admin_token=YOUR_ADMIN_SESSION_TOKEN" \
  -d '{
    "name": "User Name",
    "email": "user@example.com",
    "role": "member",
    "password": "securepassword"
  }'
```

### Password Reset (Admin-Only)

#### Web UI
1. Go to the **Users** page
2. Find the user in the list
3. Click **Copy Reset Link**
4. Send the link to the user

#### CLI
```bash
docker compose exec api python3 scripts/manage_user.py reset-password \
  --email user@example.com \
  --invite
```

#### API
```bash
curl -X POST http://localhost:8000/api/users/USER_ID/reset-password \
  --cookie "studio_admin_token=YOUR_ADMIN_SESSION_TOKEN"
# Response includes "reset_url" field
```

Reset links are valid for **72 hours** and can only be used once.

### Self-Service Password Change

Users can change their own password:

#### Web UI
1. Log in to the studio
2. Navigate to account settings (link in header)
3. Enter current password and new password
4. Click **Change Password**

#### API
```bash
curl -X POST http://localhost:8000/api/users/me/change-password \
  -H "Content-Type: application/json" \
  --cookie "studio_admin_token=YOUR_SESSION_TOKEN" \
  -d '{
    "current_password": "oldpassword",
    "new_password": "newpassword"
  }'
```

### Listing Users (Admin-Only)

#### Web UI
Navigate to the **Users** page from the studio header.

#### CLI
```bash
docker compose exec api python3 scripts/manage_user.py list
```

#### API
```bash
curl http://localhost:8000/api/users \
  --cookie "studio_admin_token=YOUR_ADMIN_SESSION_TOKEN"
```

## Login

### Web UI
Navigate to `/login` and enter:
- **Email + Password** (primary method)
- Or use the "Sign in with admin secret" toggle for the legacy admin secret

### API
```bash
# Email + password
curl -X POST http://localhost:8000/api/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "password": "userpassword"
  }'

# Admin secret (legacy)
curl -X POST http://localhost:8000/api/login \
  -H "Content-Type: application/json" \
  -d '{
    "admin_secret": "your_admin_secret"
  }'
```

Both methods set a `studio_admin_token` cookie (24-hour expiry).

### Rate Limiting

Failed email+password login attempts are rate limited:
- **5 failed attempts** per email within 5 minutes triggers a lockout
- **15 minute lockout** after exceeding the limit
- Rate limiting is **case-insensitive** (Test@Example.com and test@example.com share a counter)
- Successful login **clears** the failed attempt counter
- Admin secret login is **not rate limited**

## Logout

### Web UI
Click **Log out** in the studio header.

### API
```bash
curl -X POST http://localhost:8000/api/logout \
  --cookie "studio_admin_token=YOUR_SESSION_TOKEN"
```

Logout invalidates the session in the database and clears the cookie.

## Environment Variables

Required configuration in `.env`:

```bash
# Master secret (required, min 32 chars)
ADMIN_SECRET=your-secret-here-at-least-32-characters

# Base URL for invite/reset links (default: http://localhost:3000)
# Production: set to your server's public URL
PUBLIC_BASE_URL=http://161.35.227.250
# Or with HTTPS: PUBLIC_BASE_URL=https://your-domain.com

# Session cookie Secure flag (default: false)
# Set to 'true' when HTTPS is enabled
SESSION_COOKIE_SECURE=false
```

See `.env.example` for all configuration options.

## Security Notes

- Passwords are hashed with **bcrypt** (cost factor 12)
- Invite/reset tokens are hashed with **SHA256** before storage
- Tokens are **single-use** and expire after 72 hours
- Session tokens are **HMAC-signed** with SHA256
- Sessions have a **24-hour TTL** and are stored in the database
- Rate limiting protects against brute-force attacks
- The `SESSION_COOKIE_SECURE` flag should be enabled when HTTPS is available

## Migration from Admin Secret Only

If you're upgrading from the legacy admin-secret-only authentication:

1. Ensure `ADMIN_SECRET` is set in your `.env` file
2. Set `PUBLIC_BASE_URL` to your server's public URL (e.g., `http://161.35.227.250`)
3. Create admin users for Jack and Claire:
   ```bash
   docker compose exec api python3 scripts/manage_user.py create \
     --name "Jack" --email jack@example.com --role admin --invite
   docker compose exec api python3 scripts/manage_user.py create \
     --name "Claire" --email claire@example.com --role admin --invite
   ```
4. Send them their invite links to set passwords
5. Create member users for other team members as needed
6. Admin secret remains valid for automated scripts and CLI operations
