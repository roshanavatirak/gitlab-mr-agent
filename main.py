import logging
from typing import List, Optional
from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis import Redis
from rq import Queue

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)
logger = logging.getLogger("gitlab-mr-agent-web")

DEFAULT_CHECKLIST = (
    "Merge Reviewer Checklist:\n"
    "- Respective Code Reviewer must approve the MR.\n"
    "- All comments/discussions are resolved.\n"
    "- Pipeline is green (build, lint(code quality), tests all passed).\n"
    "- Testcases have actually run and passed.\n"
    "- No unrelated or extra files included in this MR.\n"
    "- Branch name follows the naming convention.\n"
    "- Branch tracking sheet is updated with the branch being merged.\n"
    "- MR title is clear and describes the change.\n"
    "- Description is written (what changed + why).\n"
    "- Labels are added (e.g. feature, fix, docs, test, refactor, chore).\n"
    "- Reviewer is set - Roshan.\n"
    "- Assignee is set - Developer who wrote the code.\n\n"
    "Code Reviewer Checklist:\n"
    "- Code does what was discussed/agreed for this task, nothing extra, nothing missing.\n"
    "- Coding standards followed (naming, formatting, structure, no dead/commented-out code).\n"
    "- Code follows the design doc / related doc, no deviation without discussion.\n"
    "- No duplicate logic, reused existing functions/services where possible.\n"
    "- Error handling is present, not just the happy path.\n"
    "- This MR sticks to one thing, not a mix of unrelated changes stuffed together.\n"
    "- New/changed logic has testcases covering it.\n"
    "- Developer followed the AI/vibe-coding standards, and the prompts used are logged in the tracking excel."
)

# Configuration schema using Pydantic Settings v2
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    upstash_redis_url: str = Field(..., alias="UPSTASH_REDIS_URL")
    gitlab_url: str = Field("https://gitlab.com", alias="GITLAB_URL")
    gitlab_token: str = Field(..., alias="GITLAB_TOKEN")
    webhook_secret: str = Field(..., alias="WEBHOOK_SECRET")
    
    # Unified LLM Configuration
    llm_provider: str = Field("mistral", alias="LLM_PROVIDER")
    llm_api_key: Optional[str] = Field(None, alias="LLM_API_KEY")
    llm_model: Optional[str] = Field(None, alias="LLM_MODEL")

    target_branches: str = Field("dev,main", alias="TARGET_BRANCHES")
    block_on_severity: str = Field("CRITICAL", alias="BLOCK_ON_SEVERITY")
    review_checklist: str = Field(DEFAULT_CHECKLIST, alias="REVIEW_CHECKLIST")

    # Fallback/Legacy API Keys
    mistral_api_key: Optional[str] = Field(None, alias="MISTRAL_API_KEY")
    gemini_api_key: Optional[str] = Field(None, alias="GEMINI_API_KEY")
    openai_api_key: Optional[str] = Field(None, alias="OPENAI_API_KEY")
    groq_api_key: Optional[str] = Field(None, alias="GROQ_API_KEY")
    anthropic_api_key: Optional[str] = Field(None, alias="ANTHROPIC_API_KEY")
    claude_api_key: Optional[str] = Field(None, alias="CLAUDE_API_KEY")

    @property
    def resolved_api_key(self) -> str:
        """Resolve the API key for the selected provider with legacy fallbacks."""
        if self.llm_api_key:
            return self.llm_api_key
        
        provider = self.llm_provider.lower()
        if provider == "mistral" and self.mistral_api_key:
            return self.mistral_api_key
        elif provider == "gemini" and self.gemini_api_key:
            return self.gemini_api_key
        elif provider == "openai" and self.openai_api_key:
            return self.openai_api_key
        elif provider == "groq" and self.groq_api_key:
            return self.groq_api_key
        elif provider in ("anthropic", "claude"):
            return self.anthropic_api_key or self.claude_api_key
        
        raise ValueError(f"No API key configured for provider: {self.llm_provider}")

    @property
    def resolved_model_name(self) -> str:
        """Get the model name for the selected provider."""
        if self.llm_model:
            return self.llm_model
        
        provider = self.llm_provider.lower()
        if provider == "mistral":
            return "codestral-latest"
        elif provider == "gemini":
            return "gemini-1.5-flash"
        elif provider == "openai":
            return "gpt-4o-mini"
        elif provider == "groq":
            return "llama-3.1-70b-versatile"
        elif provider in ("anthropic", "claude"):
            return "claude-3-5-sonnet-latest"
        
        raise ValueError(f"Unknown provider: {self.llm_provider}")

    @property
    def target_branches_list(self) -> List[str]:
        return [b.strip() for b in self.target_branches.split(",") if b.strip()]

# Load and validate configuration
try:
    settings = Settings()
    logger.info("Configuration successfully loaded.")
except Exception as e:
    logger.error(f"Failed to load configurations: {e}")
    # We do not crash the script on import, but raise at runtime or exit gracefully
    settings = None

app = FastAPI(title="GitLab MR Reviewer AI Agent - Web Server")

# Initialize Redis connection and RQ Queue
redis_conn = None
queue = None

if settings:
    try:
        redis_conn = Redis.from_url(settings.upstash_redis_url)
        queue = Queue("gitlab_reviews", connection=redis_conn)
        logger.info("Successfully connected to Upstash Redis queue.")
    except Exception as e:
        logger.error(f"Failed to connect to Upstash Redis: {e}")

@app.get("/")
def read_root():
    return {
        "status": "online",
        "agent": "GitLab MR Reviewer AI Agent",
        "config_loaded": settings is not None,
        "redis_connected": redis_conn is not None
    }

@app.get("/health")
def health_check():
    if not settings:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Settings not loaded"
        )
    if not redis_conn:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis disconnected"
        )
    
    try:
        redis_conn.ping()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Redis ping failed: {e}"
        )
        
    return {"status": "healthy", "redis": "connected"}

@app.post("/webhook")
async def gitlab_webhook(
    request: Request,
    x_gitlab_token: str = Header(None, alias="X-Gitlab-Token"),
    x_gitlab_event: str = Header(None, alias="X-Gitlab-Event")
):
    if not settings or not queue:
        logger.error("Web server is not fully configured. Rejecting request.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server configuration error. Redis or secrets missing."
        )

    # 1. Verify GitLab Webhook Secret Token
    if not x_gitlab_token or x_gitlab_token != settings.webhook_secret:
        logger.warning("Unauthorized webhook request. Secret token mismatch.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Webhook Secret Token"
        )

    # 2. Parse payload
    try:
        payload = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse JSON payload: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload"
        )

    # 3. Check Event Type
    # GitLab Merge Request Hook sends: X-Gitlab-Event: Merge Request Hook
    # And the payload has: object_kind: merge_request
    object_kind = payload.get("object_kind")
    if object_kind != "merge_request":
        logger.info(f"Skipping event: object_kind '{object_kind}' is not 'merge_request'")
        return {"status": "ignored", "reason": f"Unsupported object_kind '{object_kind}'"}

    # Extract target branch and action
    object_attributes = payload.get("object_attributes", {})
    target_branch = object_attributes.get("target_branch")
    action = object_attributes.get("action")
    mr_iid = object_attributes.get("iid")
    project = payload.get("project", {})
    project_id = project.get("id")

    if not project_id or not mr_iid:
        logger.warning("Missing project ID or MR IID in payload.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing project.id or object_attributes.iid"
        )

    # 4. Verify branch filtering
    allowed_branches = settings.target_branches_list
    if target_branch not in allowed_branches:
        logger.info(f"Skipping MR !{mr_iid} on project {project_id}: target branch '{target_branch}' not in {allowed_branches}")
        return {"status": "ignored", "reason": f"Target branch '{target_branch}' is not reviewed"}

    # 5. Verify action filtering
    # Actions: open, close, reopen, update, approved, merge, etc.
    # We only review when code is submitted/changed: open, reopen, update
    allowed_actions = ["open", "reopen", "update"]
    if action not in allowed_actions:
        logger.info(f"Skipping MR !{mr_iid}: action '{action}' is not in {allowed_actions}")
        return {"status": "ignored", "reason": f"MR action '{action}' does not trigger review"}

    # 6. Enqueue the task into Redis Queue
    # We pass the minimal project_id and mr_iid to avoid bloating the Redis payload.
    # The background worker will fetch current details from GitLab directly.
    try:
        job = queue.enqueue(
            "worker.process_merge_request",
            kwargs={
                "project_id": project_id,
                "mr_iid": mr_iid,
                "action": action,
                "target_branch": target_branch
            },
            job_timeout="10m",  # Generous timeout for deep code reviews
            result_ttl=3600     # Keep results for 1 hour
        )
        logger.info(f"Enqueued review job {job.id} for project {project_id} MR !{mr_iid} (action: {action})")
        return {
            "status": "enqueued",
            "job_id": job.id,
            "project_id": project_id,
            "mr_iid": mr_iid,
            "action": action
        }
    except Exception as e:
        logger.error(f"Failed to enqueue job to Upstash Redis: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Task queuing failed: {e}"
        )
