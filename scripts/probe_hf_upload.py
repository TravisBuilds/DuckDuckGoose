#!/usr/bin/env python3
"""
Probe script to test Higgsfield upload API and discover the real URL format.

Usage:
    export HIGGSFIELD_API_KEY="your_id:your_secret"
    python scripts/probe_hf_upload.py

This script:
1. Generates a small test image
2. Calls /files/generate-upload-url with content_type
3. PUTs the file to the returned upload_url with upload_headers
4. Reports the public_url for use in generation endpoints
"""

import asyncio
import httpx
import os
import sys
from pathlib import Path


async def probe_upload():
    api_key = os.getenv("HIGGSFIELD_API_KEY")
    if not api_key or ":" not in api_key:
        print("ERROR: HIGGSFIELD_API_KEY not set or invalid format")
        print("Expected format: id:secret")
        sys.exit(1)
    
    api_id, api_secret = api_key.split(":", 1)
    base_url = os.getenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    print(f"Probing Higgsfield upload API at {base_url}")
    print(f"Using API ID: {api_id[:10]}...")
    print()
    
    # Create a minimal test image (1x1 PNG)
    test_image = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x00\x00\x00\x00IEND\xaeB`\x82'
    
    client = httpx.AsyncClient(
        base_url=base_url,
        headers={
            "Authorization": f"Key {api_id}:{api_secret}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    
    try:
        # Step 1: Get upload URL (REQUIRES content_type)
        print("Step 1: POST /files/generate-upload-url with content_type")
        response = await client.post(
            "/files/generate-upload-url",
            json={"content_type": "image/png"}
        )
        response.raise_for_status()
        upload_data = response.json()
        
        print(f"  Response keys: {list(upload_data.keys())}")
        print(f"  public_url: {upload_data.get('public_url', 'N/A')}")
        print()
        
        upload_url = upload_data.get("upload_url")
        public_url = upload_data.get("public_url")
        upload_headers = upload_data.get("upload_headers", {})
        
        if not upload_url or not public_url:
            print("ERROR: Missing upload_url or public_url in response")
            sys.exit(1)
        
        # Step 2: PUT file to upload URL with exact headers
        print(f"Step 2: PUT to upload_url with upload_headers")
        print(f"  upload_headers: {upload_headers}")
        
        async with httpx.AsyncClient() as plain_client:
            put_response = await plain_client.put(
                upload_url,
                content=test_image,
                headers=upload_headers
            )
            put_response.raise_for_status()
            
            print(f"  Status: {put_response.status_code}")
            print(f"  Response body: {put_response.text if put_response.text else '(empty)'}")
        
        # Step 3: Verify public_url is accessible
        print()
        print(f"Step 3: GET {public_url}")
        async with httpx.AsyncClient() as plain_client:
            get_response = await plain_client.get(public_url)
            get_response.raise_for_status()
            print(f"  Status: {get_response.status_code}")
            print(f"  Content-Type: {get_response.headers.get('content-type')}")
            print(f"  Content-Length: {len(get_response.content)} bytes")
            print(f"  Bytes match: {get_response.content == test_image}")
        
        print()
        print("=" * 80)
        print("RESULTS:")
        print("=" * 80)
        print(f"✓ Upload successful")
        print(f"✓ public_url: {public_url}")
        print(f"✓ Use public_url in generation requests (image_urls list)")
        print()
        print("API Contract:")
        print("  1. POST /files/generate-upload-url requires {content_type}")
        print("  2. Response has {public_url, upload_url, upload_headers}")
        print("  3. PUT upload_url with exact upload_headers (includes x-amz-tagging)")
        print("  4. Use public_url in generation payloads")
        
    except httpx.HTTPStatusError as e:
        print(f"HTTP Error: {e.response.status_code}")
        print(f"Response: {e.response.text}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(probe_upload())
