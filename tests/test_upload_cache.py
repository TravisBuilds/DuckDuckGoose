"""Tests for upload cache functionality."""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import respx

from hfvg.upload_cache import UploadCache
from hfvg.providers.higgsfield_still import HiggsfieldStillProvider


@pytest.mark.asyncio
async def test_upload_cache_reuses_url_across_submits(tmp_path):
    """Test that the same file is only uploaded once and the cached URL is reused."""
    
    # Create a test image file
    ref_path = tmp_path / "duck.jpg"
    ref_path.write_bytes(b"fake-duck-image-data")
    
    # Setup test environment
    db_path = str(tmp_path / "test.db")
    
    # Initialize cache
    cache = UploadCache(db_path)
    await cache.init_db()
    
    # Mock Higgsfield API
    with respx.mock:
        # Mock upload URL generation (called only ONCE)
        # Verify content_type is sent in request
        upload_url_calls = []
        
        def capture_upload_url_request(request):
            import json
            upload_url_calls.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "upload_url": "http://mock.upload/put-here",
                    "public_url": "http://mock.cdn/abc123.jpg",
                    "upload_headers": {
                        "Content-Type": "image/jpeg",
                        "x-amz-tagging": "retention=temporary"
                    }
                }
            )
        
        upload_url_route = respx.post("http://mock.api/files/generate-upload-url").mock(
            side_effect=capture_upload_url_request
        )
        
        # Mock the actual upload (called only ONCE)
        # Verify upload_headers are sent
        upload_calls = []
        
        def capture_upload_request(request):
            upload_calls.append({
                "content": request.content,
                "headers": dict(request.headers)
            })
            return httpx.Response(200, content=b"")
        
        upload_route = respx.put("http://mock.upload/put-here").mock(
            side_effect=capture_upload_request
        )
        
        # Mock HEAD check for cached URL (called on second submit)
        head_route = respx.head("http://mock.cdn/abc123.jpg").mock(
            return_value=httpx.Response(200)
        )
        
        # Setup provider
        import os
        os.environ["HIGGSFIELD_API_KEY"] = "test_id:test_secret"
        os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
        os.environ["DATABASE_PATH"] = db_path
        os.environ["SKIP_IMAGE_NORMALIZE"] = "true"
        
        provider = HiggsfieldStillProvider()
        
        # First upload - should actually upload
        result1 = await provider.upload_reference(str(ref_path), version=1)
        assert result1["public_url"] == "http://mock.cdn/abc123.jpg"
        assert upload_url_route.call_count == 1
        assert upload_route.call_count == 1
        
        # Verify content_type was sent in the POST request
        assert len(upload_url_calls) == 1
        assert "content_type" in upload_url_calls[0], "content_type must be included in upload URL request"
        assert upload_url_calls[0]["content_type"] == "image/jpeg"
        
        # Verify upload_headers were sent in the PUT request
        assert len(upload_calls) == 1
        assert "content-type" in upload_calls[0]["headers"]
        assert "x-amz-tagging" in upload_calls[0]["headers"]
        
        # Second upload with same file and version - should use cache
        result2 = await provider.upload_reference(str(ref_path), version=1)
        assert result2["public_url"] == "http://mock.cdn/abc123.jpg"
        # Upload should NOT be called again
        assert upload_url_route.call_count == 1, "Upload URL should only be requested once"
        assert upload_route.call_count == 1, "File should only be uploaded once"
        # HEAD check should be called to verify cached URL
        assert head_route.call_count == 1, "HEAD check should be called for cached URL"


@pytest.mark.asyncio
async def test_upload_cache_reupload_on_head_failure(tmp_path):
    """Test that cached URL is re-uploaded when HEAD check returns 404."""
    
    # Create a test image file
    ref_path = tmp_path / "duck.jpg"
    ref_path.write_bytes(b"fake-duck-image-data")
    
    # Setup test environment
    db_path = str(tmp_path / "test.db")
    
    # Initialize cache
    cache = UploadCache(db_path)
    await cache.init_db()
    
    # Mock Higgsfield API
    with respx.mock:
        # Mock upload URL generation (called TWICE - initial + re-upload)
        upload_url_route = respx.post("http://mock.api/files/generate-upload-url").mock(
            return_value=httpx.Response(
                200,
                json={
                    "upload_url": "http://mock.upload/put-here",
                    "public_url": "http://mock.cdn/abc123.jpg",
                    "upload_headers": {
                        "Content-Type": "image/jpeg",
                        "x-amz-tagging": "retention=temporary"
                    }
                }
            )
        )
        
        # Mock the actual upload (called TWICE)
        upload_route = respx.put("http://mock.upload/put-here").mock(
            return_value=httpx.Response(200, content=b"")
        )
        
        # Mock HEAD check - returns 404 (expired/deleted)
        head_route = respx.head("http://mock.cdn/abc123.jpg").mock(
            return_value=httpx.Response(404)
        )
        
        # Setup provider
        import os
        os.environ["HIGGSFIELD_API_KEY"] = "test_id:test_secret"
        os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
        os.environ["DATABASE_PATH"] = db_path
        os.environ["SKIP_IMAGE_NORMALIZE"] = "true"
        
        provider = HiggsfieldStillProvider()
        
        # First upload
        result1 = await provider.upload_reference(str(ref_path), version=1)
        assert result1["public_url"] == "http://mock.cdn/abc123.jpg"
        assert upload_url_route.call_count == 1
        assert upload_route.call_count == 1
        
        # Second upload - HEAD check fails, should re-upload
        result2 = await provider.upload_reference(str(ref_path), version=1)
        assert result2["public_url"] == "http://mock.cdn/abc123.jpg"
        # HEAD check should be called
        assert head_route.call_count == 1, "HEAD check should be called"
        # Upload should be called again after HEAD check failure
        assert upload_url_route.call_count == 2, "Upload URL should be requested twice"
        assert upload_route.call_count == 2, "File should be uploaded twice (re-upload)"


@pytest.mark.asyncio
async def test_upload_cache_different_version_uploads_separately(tmp_path):
    """Test that different versions of the same file are cached separately."""
    
    # Create a test image file
    ref_path = tmp_path / "duck.jpg"
    ref_path.write_bytes(b"fake-duck-image-data")
    
    # Setup test environment
    db_path = str(tmp_path / "test.db")
    
    # Initialize cache
    cache = UploadCache(db_path)
    await cache.init_db()
    
    # Mock Higgsfield API
    with respx.mock:
        # Mock upload URL generation (called TWICE for v1 and v2)
        upload_url_route = respx.post("http://mock.api/files/generate-upload-url").mock(
            return_value=httpx.Response(
                200,
                json={
                    "upload_url": "http://mock.upload/put-here",
                    "public_url": "http://mock.cdn/abc123.jpg",
                    "upload_headers": {
                        "Content-Type": "image/jpeg",
                        "x-amz-tagging": "retention=temporary"
                    }
                }
            )
        )
        
        # Mock the actual upload (called TWICE)
        upload_route = respx.put("http://mock.upload/put-here").mock(
            return_value=httpx.Response(200, content=b"")
        )
        
        # Mock HEAD check
        head_route = respx.head("http://mock.cdn/abc123.jpg").mock(
            return_value=httpx.Response(200)
        )
        
        # Setup provider
        import os
        os.environ["HIGGSFIELD_API_KEY"] = "test_id:test_secret"
        os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
        os.environ["DATABASE_PATH"] = db_path
        os.environ["SKIP_IMAGE_NORMALIZE"] = "true"
        
        provider = HiggsfieldStillProvider()
        
        # Upload v1
        result1 = await provider.upload_reference(str(ref_path), version=1)
        assert result1["public_url"] == "http://mock.cdn/abc123.jpg"
        assert upload_url_route.call_count == 1
        
        # Upload v2 (different version) - should upload again
        result2 = await provider.upload_reference(str(ref_path), version=2)
        assert result2["public_url"] == "http://mock.cdn/abc123.jpg"
        assert upload_url_route.call_count == 2, "Different version should trigger new upload"
        assert upload_route.call_count == 2


@pytest.mark.asyncio
async def test_upload_cache_reupload_on_head_403(tmp_path):
    """Test that cached URL is re-uploaded when HEAD check returns 403 (forbidden)."""
    
    # Create a test image file
    ref_path = tmp_path / "duck.jpg"
    ref_path.write_bytes(b"fake-duck-image-data")
    
    # Setup test environment
    db_path = str(tmp_path / "test.db")
    
    # Initialize cache
    cache = UploadCache(db_path)
    await cache.init_db()
    
    # Mock Higgsfield API
    with respx.mock:
        # Mock upload URL generation (called TWICE)
        upload_url_route = respx.post("http://mock.api/files/generate-upload-url").mock(
            return_value=httpx.Response(
                200,
                json={
                    "upload_url": "http://mock.upload/put-here",
                    "public_url": "http://mock.cdn/abc123.jpg",
                    "upload_headers": {
                        "Content-Type": "image/jpeg",
                        "x-amz-tagging": "retention=temporary"
                    }
                }
            )
        )
        
        # Mock the actual upload (called TWICE)
        upload_route = respx.put("http://mock.upload/put-here").mock(
            return_value=httpx.Response(200, content=b"")
        )
        
        # Mock HEAD check - returns 403 (forbidden/expired credentials)
        head_route = respx.head("http://mock.cdn/abc123.jpg").mock(
            return_value=httpx.Response(403)
        )
        
        # Setup provider
        import os
        os.environ["HIGGSFIELD_API_KEY"] = "test_id:test_secret"
        os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
        os.environ["DATABASE_PATH"] = db_path
        os.environ["SKIP_IMAGE_NORMALIZE"] = "true"
        
        provider = HiggsfieldStillProvider()
        
        # First upload
        result1 = await provider.upload_reference(str(ref_path), version=1)
        assert result1["public_url"] == "http://mock.cdn/abc123.jpg"
        
        # Second upload - HEAD check fails with 403, should re-upload
        result2 = await provider.upload_reference(str(ref_path), version=1)
        assert result2["public_url"] == "http://mock.cdn/abc123.jpg"
        assert head_route.call_count == 1
        assert upload_url_route.call_count == 2, "Should re-upload after 403"
        assert upload_route.call_count == 2
