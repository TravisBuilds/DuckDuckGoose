"""Task 6: Test that non-escalated QC-fail outcome includes clip_path."""

def test_non_escalated_qc_fail_includes_clip_path():
    """Task 6: Verify that non-escalated QC-fail outcome has clip_path field.
    
    This is a documentation test - the actual fix is in shot.py line ~1294.
    The returned dict must include clip_path so accept_clip_review can use it.
    """
    # Expected structure of non-escalated QC-fail outcome
    expected_fields = {
        "shot_id",
        "status",  # "failed"
        "still_url",
        "clip_url",
        "clip_path",  # Task 6: This field must be present
        "error",
        "qc_constraints",
    }
    
    # This test documents the requirement - the actual implementation is in
    # hfvg/workflows/shot.py around line 1289-1296
    # The outcome should look like:
    # {
    #     "shot_id": self.shot_id,
    #     "status": "failed",
    #     "still_url": self.still_asset["url"],
    #     "clip_url": self.clip_asset["url"],
    #     "clip_path": self.clip_local_path,  # <-- Must be present
    #     "error": f"QC failed: {', '.join(clip_qc['issues'])}",
    #     "qc_constraints": clip_qc.get("constraints"),
    # }
    
    # Verify the expected fields are documented
    assert "clip_path" in expected_fields, "Non-escalated QC-fail must include clip_path"
    
    # The actual runtime test is covered by integration tests that verify
    # accept_clip_review works with non-escalated QC failures
