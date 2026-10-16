#!/usr/bin/env python3
"""
Probe script to test Higgsfield upload API and discover the real URL format.

Usage:
    export HIGGSFIELD_API_KEY="your_id:your_secret"
    python scripts/probe_hf_upload.py

This script:
1. Generates a small test image
2. Calls /files/generate-upload-url
3. PUTs the file to the returned upload_url
4. Reports what URL format should be used for generation endpoints
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
        # Step 1: Get upload URL
        print("Step 1: POST /files/generate-upload-url")
        response = await client.post(
            "/files/generate-upload-url",
            json={"filename": "probe_test.png"}
        )
        response.raise_for_status()
        upload_data = response.json()
        
        print(f"  Response: {upload_data}")
        print()
        
        upload_url = upload_data.get("upload_url")
        media_id = upload_data.get("media_id")
        file_url = upload_data.get("file_url") or upload_data.get("url")
        
        if not upload_url:
            print("ERROR: No upload_url in response")
            sys.exit(1)
        
        # Step 2: PUT file to upload URL
        print(f"Step 2: PUT to upload_url")
        print(f"  URL: {upload_url[:80]}...")
        
        async with httpx.AsyncClient() as plain_client:
            put_response = await plain_client.put(
                upload_url,
                content=test_image,
                headers={"Content-Type": "image/png"}
            )
            put_response.raise_for_status()
            
            print(f"  Status: {put_response.status_code}")
            print(f"  Response body: {put_response.text if put_response.text else '(empty)'}")
            
            # Check if PUT response contains URL
            if put_response.text:
                try:
                    put_data = put_response.json()
                    print(f"  JSON response: {put_data}")
                    if "url" in put_data:
                        file_url = put_data["url"]
                except:
                    pass
        
        print()
        print("=" * 80)
        print("RESULTS:")
        print("=" * 80)
        print(f"media_id: {media_id}")
        print(f"file_url from initial response: {file_url}")
        print()
        
        if file_url:
            print("✓ File URL available from API response")
            print(f"  Use this URL in generation requests: {file_url}")
        elif media_id:
            print("✓ Media ID available")
            print(f"  Constructed URL format: {base_url}/files/{media_id}")
            print(f"  OR pass media_id directly if API supports it")
        else:
            print("✗ No clear URL format found")
        
        print()
        print("Recommendation for code:")
        if file_url:
            print("  1. Use 'file_url' or 'url' field from generate-upload-url response")
            print("  2. If not present in initial response, check PUT response")
        else:
            print("  1. Construct URL as: {base_url}/files/{media_id}")
        
    except httpx.HTTPStatusError as e:
        print(f"HTTP Error: {e.response.status_code}")
        print(f"Response: {e.response.text}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(probe_upload())
