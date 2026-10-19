"""Test /api/models endpoint security."""
import pytest


def test_api_models_no_local_filesystem_paths():
    """API models endpoint must not expose local filesystem paths in sources."""
    from hfvg import model_caps
    
    # Get a model that has sources
    view = model_caps.creator_view("xai/grok-imagine-image-2.0")
    
    # Check all limits
    for limit_name, limit_data in view["limits"].items():
        if "sources" in limit_data:
            sources = limit_data["sources"]
            for source in sources:
                url = source.get("url", "")
                # Must be http(s) only, no local paths
                assert url.startswith("https://") or url.startswith("http://"), (
                    f"Model {view['model_id']} limit {limit_name} has non-HTTP source: {url}"
                )
                # Must not contain filesystem indicators
                assert not url.startswith("/"), f"Local path exposed: {url}"
                assert not url.startswith("./"), f"Local path exposed: {url}"
                assert not url.startswith("file://"), f"Local path exposed: {url}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
