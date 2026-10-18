"""Activity to save completed stills and clips locally."""

import os
import httpx
from pathlib import Path
from temporalio import activity


@activity.defn
async def save_asset_locally(
    episode_id: str,
    shot_id: str,
    asset_url: str,
    asset_type: str,  # "still" or "clip"
    attempt: int = 1,
) -> str:
    """
    Download and save a completed still or clip to the episode's output directory.
    
    Args:
        episode_id: Episode ID (e.g., "ep04")
        shot_id: Shot ID (e.g., "A01")
        asset_url: URL of the asset to download
        asset_type: "still" or "clip"
        attempt: Attempt number for this shot
        
    Returns:
        Local file path where the asset was saved
    """
    # Determine extension from URL or content-type
    ext = ".jpg"  # Default for stills
    if asset_type == "clip":
        ext = ".mp4"  # Default for clips
    elif asset_url.endswith(".png"):
        ext = ".png"
    elif asset_url.endswith(".webp"):
        ext = ".webp"
    
    # Create output directory structure: data/episodes/{episode}/outputs/{shot}/
    output_dir = Path("data/episodes") / episode_id / "outputs" / shot_id
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Filename: still_v1.jpg, clip_v1.mp4, etc.
    filename = f"{asset_type}_v{attempt}{ext}"
    output_path = output_dir / filename
    
    # Download the asset
    activity.logger.info(f"Downloading {asset_type} from {asset_url} to {output_path}")
    
    try:
        # Handle local file paths (dry run mode)
        if not asset_url.startswith(("http://", "https://")):
            # It's a local file path, just copy it
            if Path(asset_url).exists():
                output_path.write_bytes(Path(asset_url).read_bytes())
                activity.logger.info(f"Copied local file {asset_url} to {output_path}")
                return str(output_path)
            else:
                activity.logger.warning(f"Local file {asset_url} does not exist, skipping")
                # Return a placeholder path for dry run
                return str(output_path)
        
        # HTTP download for real assets
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.get(asset_url)
            response.raise_for_status()
            
            # Try to get extension from Content-Type header
            content_type = response.headers.get("content-type", "")
            if asset_type == "still":
                if "png" in content_type:
                    ext = ".png"
                elif "webp" in content_type:
                    ext = ".webp"
                elif "jpeg" in content_type or "jpg" in content_type:
                    ext = ".jpg"
            elif asset_type == "clip":
                if "webm" in content_type:
                    ext = ".webm"
                elif "mp4" in content_type:
                    ext = ".mp4"
            
            # Update filename with correct extension if it changed
            if not filename.endswith(ext):
                filename = f"{asset_type}_v{attempt}{ext}"
                output_path = output_dir / filename
            
            # Write to file
            output_path.write_bytes(response.content)
            
            activity.logger.info(f"Saved {asset_type} to {output_path}")
            return str(output_path)
            
    except Exception as e:
        activity.logger.error(f"Failed to download {asset_type} from {asset_url}: {e}")
        raise
