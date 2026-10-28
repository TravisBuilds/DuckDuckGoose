"""Upload cache for Higgsfield reference images.

Caches public_url per (path, version, sha256) to avoid re-uploading the same file.
Before reuse, HEAD-checks the URL and re-uploads if it returns non-200.

Concurrent upload deduplication:
- Uses DB unique constraint + retry loop to prevent parallel uploads of same file
- Only one upload proceeds; others wait and reuse the result
"""

import asyncio
import hashlib
import os
from pathlib import Path
from datetime import datetime, timezone

import aiosqlite
import httpx


class UploadCache:
    """
    Cache for uploaded reference images.
    
    Tracks (path, version, sha256) -> public_url with created_at timestamp.
    Before reuse, HEAD-checks the URL to ensure it's still accessible.
    """
    
    def __init__(self, db_path: str | None = None):
        """
        Initialize upload cache.
        
        Args:
            db_path: Path to SQLite database (default: DATABASE_PATH env or ./data/studio.db)
        """
        self.db_path = db_path or os.getenv("DATABASE_PATH", "./data/studio.db")
    
    async def init_db(self):
        """Initialize database table for upload cache."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_cache (
                    file_sha256 TEXT NOT NULL,
                    file_version INTEGER NOT NULL,
                    public_url TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    file_path TEXT,
                    PRIMARY KEY (file_sha256, file_version)
                )
                """
            )
            await db.commit()
    
    def _compute_sha256(self, file_path: str | Path) -> str:
        """Compute SHA256 hash of file contents."""
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    
    async def get_cached_url(self, file_path: str, version: int) -> str | None:
        """
        Get cached public_url for a file, verifying it's still accessible.
        
        Args:
            file_path: Path to local file
            version: Version number for the file
        
        Returns:
            Cached public_url if it exists and is accessible, None otherwise
        """
        # Compute SHA256 of current file
        sha256 = self._compute_sha256(file_path)
        
        # Look up in cache by sha256+version only (dedupes across different paths)
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT public_url FROM upload_cache WHERE file_sha256 = ? AND file_version = ?",
                (sha256, version)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                
                public_url = row[0]
        
        # Treat uploading://in-progress as a miss - never HEAD-check or delete it
        if public_url == "uploading://in-progress":
            return None
        
        # HEAD-check the URL to ensure it's still accessible
        try:
            async with httpx.AsyncClient() as client:
                response = await client.head(public_url, timeout=10.0)
                if response.status_code == 200:
                    return public_url
                else:
                    # URL is not accessible, remove from cache
                    await self.remove_cached_url(file_path, version, sha256)
                    return None
        except (httpx.HTTPError, Exception):
            # HEAD check failed, remove from cache
            await self.remove_cached_url(file_path, version, sha256)
            return None
    
    async def cache_url(self, file_path: str, version: int, public_url: str):
        """
        Cache a public_url for a file.
        
        Args:
            file_path: Path to local file
            version: Version number for the file
            public_url: Public CDN URL for the uploaded file
        """
        # Compute SHA256 of file
        sha256 = self._compute_sha256(file_path)
        
        # Store in cache with current timestamp (key: sha256+version for deduplication)
        created_at = datetime.now(timezone.utc).isoformat()
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO upload_cache 
                (file_sha256, file_version, public_url, created_at, file_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sha256, version, public_url, created_at, file_path)
            )
            await db.commit()
    
    async def try_claim_upload_slot(self, file_path: str, version: int) -> tuple[bool, str]:
        """
        Try to claim an upload slot for concurrent deduplication.
        
        Uses BEGIN IMMEDIATE for exclusive lock to prevent race conditions
        in concurrent uploads of the same file.
        
        Returns:
            (claimed, sha256): If claimed=True, caller should upload. If False, wait and retry get_cached_url.
        """
        sha256 = self._compute_sha256(file_path)
        created_at = datetime.now(timezone.utc).isoformat()
        
        # Try to insert a placeholder with special "uploading://" URL (key: sha256+version)
        # Use BEGIN IMMEDIATE to get exclusive write lock before INSERT, preventing race
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    """
                    INSERT INTO upload_cache 
                    (file_sha256, file_version, public_url, created_at, file_path)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (sha256, version, "uploading://in-progress", created_at, file_path)
                )
                await db.commit()
                return (True, sha256)  # Claimed! Caller must upload
            except aiosqlite.IntegrityError:
                # Another upload already claimed this slot
                await db.rollback()
                return (False, sha256)  # Wait for the other upload to complete
    
    async def wait_for_concurrent_upload(self, file_path: str, version: int, sha256: str, timeout: float = 30.0) -> str | None:
        """
        Wait for a concurrent upload to complete and return its URL.
        
        Args:
            file_path: Path to file
            version: Version number  
            sha256: SHA256 hash
            timeout: Max seconds to wait
        
        Returns:
            public_url if upload completed, None if timeout or failed
        """
        start = asyncio.get_event_loop().time()
        
        while (asyncio.get_event_loop().time() - start) < timeout:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT public_url FROM upload_cache WHERE file_sha256 = ? AND file_version = ?",
                    (sha256, version)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        url = row[0]
                        if url != "uploading://in-progress":
                            # Upload completed!
                            return url
            
            # Still uploading, wait a bit
            await asyncio.sleep(0.2)
        
        # Timeout - clean up the placeholder and let caller retry
        await self.remove_cached_url(file_path, version, sha256)
        return None
    
    async def remove_cached_url(self, file_path: str, version: int, sha256: str):
        """
        Remove a cached URL (called when HEAD check fails).
        
        Args:
            file_path: Path to local file
            version: Version number
            sha256: SHA256 hash of file
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM upload_cache WHERE file_sha256 = ? AND file_version = ?",
                (sha256, version)
            )
            await db.commit()
