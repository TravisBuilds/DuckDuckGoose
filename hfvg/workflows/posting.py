"""PostingWorkflow: handles batched post approval queue."""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from hfvg.activities import post_to_platform


@workflow.defn
class PostingWorkflow:
    """Workflow for batched post approvals and publishing."""

    def __init__(self):
        self.posts: list[dict] = []
        self.approved_posts: list[str] = []
        self.post_approval_received = False

    @workflow.run
    async def run(self, episode_id: str, video_url: str, platforms: list[str]) -> dict:
        """
        Handle post approval queue and publishing.

        Args:
            episode_id: Episode identifier
            video_url: URL of finished video
            platforms: List of platforms to post to

        Returns:
            dict with posted URLs per platform
        """
        workflow.logger.info(f"PostingWorkflow for episode {episode_id}")

        self.posts = [
            {
                "platform": platform,
                "video_url": video_url,
                "caption": f"New video from episode {episode_id}",
                "metadata": {},
            }
            for platform in platforms
        ]

        await workflow.wait_condition(lambda: self.post_approval_received)

        workflow.logger.info("Post approval received, publishing...")

        posted_urls = {}
        for post in self.posts:
            if post["platform"] in self.approved_posts:
                url = await workflow.execute_activity(
                    post_to_platform,
                    args=[
                        post["video_url"],
                        post["platform"],
                        post["caption"],
                        post["metadata"],
                    ],
                    start_to_close_timeout=timedelta(minutes=5),
                    heartbeat_timeout=timedelta(minutes=1),
                )
                posted_urls[post["platform"]] = url

        workflow.logger.info(f"Posted to {len(posted_urls)} platforms")
        return {"status": "completed", "posts": posted_urls}

    @workflow.update
    async def approve_posts(self, platforms: list[str]) -> str:
        """Approve posts for specified platforms."""
        self.approved_posts = platforms
        self.post_approval_received = True
        workflow.logger.info(f"Approved posts for platforms: {platforms}")
        return "approved"

    @workflow.query
    def get_pending_posts(self) -> list[dict]:
        """Query pending posts."""
        return self.posts
