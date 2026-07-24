import json
import logging
import traceback
from typing import List, Optional
from pydantic import BaseModel, Field
import gitlab
import httpx
from main import settings

# Configure logger
logger = logging.getLogger("gitlab-mr-agent-worker")

# Define Pydantic Models for Structured Output
class LineComment(BaseModel):
    file_path: str = Field(
        ..., 
        description="The relative path of the file to comment on, e.g., 'src/main.py'."
    )
    line_number: Optional[int] = Field(
        None, 
        description="The line number in the NEW version of the file where the issue resides. Must be a line added or modified in the diff (prefixed with '+'). Leave null/empty only if the comment applies to the file as a whole rather than a specific line."
    )
    severity: str = Field(
        ..., 
        description="Severity: 'CRITICAL' (security/logic bugs), 'WARNING' (performance/bugs), 'SUGGESTION' (best practices), 'STYLE' (formatting/readability)."
    )
    issue_description: str = Field(
        ..., 
        description="Detailed description of what the issue is, why it is problematic, and how it impacts the system."
    )
    suggestion: str = Field(
        ..., 
        description="A concrete, drop-in replacement code snippet or a step-by-step fix suggestion."
    )

class ReviewResult(BaseModel):
    summary: str = Field(
        ..., 
        description="High-level overview of the merge request review, highlighting key strengths and major areas of concern."
    )
    checklist_status: str = Field(
        ..., 
        description="A summary of how the code fared against the custom review checklist (e.g., 'All security and performance checks passed', 'Failed security checklist due to hardcoded credential')."
    )
    comments: List[LineComment] = Field(
        ..., 
        description="List of specific, line-by-line review comments."
    )
    should_approve: bool = Field(
        ..., 
        description="True if there are NO 'CRITICAL' severity issues. False if one or more CRITICAL issues are identified."
    )

def process_merge_request(project_id: int, mr_iid: int, action: str, target_branch: str):
    """
    Main job worker execution loop.
    1. Authenticates with GitLab.
    2. Gathers MR details and diff.
    3. Triggers Stage 1 (Initial Review) and Stage 2 (Self-Critique/Refining) via Gemini.
    4. Posts inline comments on GitLab (with fallback to summary).
    5. Posts summary and handles approvals.
    """
    logger.info(f"Starting review processing for Project {project_id} MR !{mr_iid}")

    if not settings:
        logger.error("Settings not loaded. Cannot run background job.")
        return

    try:
        # 1. Connect to GitLab
        gl = gitlab.Gitlab(url=settings.gitlab_url, private_token=settings.gitlab_token)
        gl.auth()
        
        project = gl.projects.get(project_id)
        mr = project.mergerequests.get(mr_iid)

        logger.info(f"Retrieved MR: '{mr.title}' targeting branch '{mr.target_branch}'")

        # 2. Get MR Changes (Diffs)
        mr_changes = mr.changes()
        changes_list = mr_changes.get("changes", [])
        
        if not changes_list:
            logger.info(f"No changes/files found in MR !{mr_iid}. Completing job.")
            return

        # 3. Format Diffs for LLM
        formatted_diffs = []
        for change in changes_list:
            old_path = change.get("old_path")
            new_path = change.get("new_path")
            diff_text = change.get("diff")
            
            if change.get("deleted_file"):
                formatted_diffs.append(f"--- FILE DELETED: {old_path} ---\n")
                continue
                
            header = f"--- FILE: {new_path} "
            if change.get("new_file"):
                header += "(NEW FILE) "
            header += "---\n"
            
            formatted_diffs.append(header + (diff_text or ""))

        diff_payload = "\n\n".join(formatted_diffs)
        
        # Protect against massive payload (basic truncation to avoid API limits)
        max_chars = 300000
        if len(diff_payload) > max_chars:
            logger.warning(f"Diff payload size ({len(diff_payload)} chars) exceeds limit. Truncating.")
            diff_payload = diff_payload[:max_chars] + "\n\n... [TRUNCATED DUE TO SIZE] ..."

        # 4. Invoke Gemini AI Agent Loop
        review_data = run_agent_review_loop(diff_payload, mr.title, mr.description or "")
        
        if not review_data:
            logger.error("AI review failed to produce a valid review result.")
            return

        # 5. Post comments to GitLab
        post_review_comments(mr, review_data)

    except Exception as e:
        logger.error(f"Error executing worker job: {e}")
        logger.error(traceback.format_exc())

def clean_json_string(text: str) -> str:
    """Strip markdown code block wrappers (like ```json ... ```) from a string."""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline:].strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    return text

def call_llm_provider(provider: str, model: str, api_key: str, system_instr: str, user_prompt: str) -> str:
    """Invokes chat completion REST endpoint for the selected provider."""
    p = provider.lower()
    
    if p in ("openai", "groq", "mistral"):
        if p == "openai":
            url = "https://api.openai.com/v1/chat/completions"
        elif p == "groq":
            url = "https://api.groq.com/openai/v1/chat/completions"
        else:
            url = "https://api.mistral.ai/v1/chat/completions"
            
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_instr},
                {"role": "user", "content": user_prompt}
            ],
            "response_format": {"type": "json_object"}
        }
        
    elif p == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        headers = {
            "Content-Type": "application/json"
        }
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": f"System Instruction:\n{system_instr}\n\nUser Request:\n{user_prompt}"}
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json"
            }
        }
        
    elif p in ("anthropic", "claude"):
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        payload = {
            "model": model,
            "max_tokens": 4096,
            "system": system_instr,
            "messages": [
                {"role": "user", "content": user_prompt}
            ]
        }
        
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")

    with httpx.Client(timeout=120.0) as client:
        r = client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        resp_json = r.json()
        
        if p in ("openai", "groq", "mistral"):
            return resp_json["choices"][0]["message"]["content"]
        elif p == "gemini":
            return resp_json["candidates"][0]["content"]["parts"][0]["text"]
        elif p in ("anthropic", "claude"):
            return resp_json["content"][0]["text"]
            
    raise RuntimeError(f"Failed to fetch content from {provider}")

def run_agent_review_loop(diff_text: str, mr_title: str, mr_description: str) -> Optional[ReviewResult]:
    """
    Two-stage agentic reasoning loop supporting multiple LLM providers:
    Stage 1: Multi-step code review draft generation.
    Stage 2: Self-critique and alignment verification.
    """
    try:
        provider = settings.llm_provider
        api_key = settings.resolved_api_key
        model = settings.resolved_model_name
        
        system_instruction = (
            "You are 'Antigravity Reviewer', an elite Principal Software Engineer & Security Auditor AI Agent. "
            "Your task is to conduct a highly thorough code review of the provided code diff.\n\n"
            "Review Guidelines:\n"
            "1. Be precise, constructive, and technically accurate. Avoid generic feedback.\n"
            "2. Read the changes carefully and identify critical errors, performance bottlenecks, or security holes.\n"
            "3. Enforce the user's checklist. Do not miss any items.\n"
            "4. Only comment on line numbers that exist in the new version of the code and were added/modified in the diff (prefixed with '+'). "
            "Verify this line number mapping carefully.\n"
            "5. Always suggest concrete code fixes using Markdown blocks inside the suggestion field.\n"
            "6. You MUST respond with a valid JSON object matching this schema:\n"
            "{\n"
            "  \"summary\": \"High-level review summary\",\n"
            "  \"checklist_status\": \"Status of checklist checks\",\n"
            "  \"comments\": [\n"
            "    {\n"
            "      \"file_path\": \"relative file path\",\n"
            "      \"line_number\": 12,\n"
            "      \"severity\": \"CRITICAL / WARNING / SUGGESTION / STYLE\",\n"
            "      \"issue_description\": \"detailed description\",\n"
            "      \"suggestion\": \"concrete fix suggestions\"\n"
            "    }\n"
            "  ],\n"
            "  \"should_approve\": true / false\n"
            "}"
        )

        # STAGE 1: Gather and analyze
        prompt = (
            f"Merge Request Title: {mr_title}\n"
            f"Merge Request Description: {mr_description}\n\n"
            f"Target Review Checklist:\n{settings.review_checklist}\n\n"
            f"Code Diff:\n{diff_text}\n\n"
            "Perform a detailed code review. Think step-by-step to identify issues. "
            "Draft your findings. Focus on security, correctness, and adherence to the checklist."
        )

        logger.info(f"Stage 1: Requesting code review draft from {provider} ({model})...")
        stage1_raw = call_llm_provider(provider, model, api_key, system_instruction, prompt)
        stage1_clean = clean_json_string(stage1_raw)
        
        # Parse initial result to log intermediate thoughts
        initial_result = ReviewResult.parse_raw(stage1_clean)
        logger.info(f"Stage 1 review completed. Found {len(initial_result.comments)} draft comments.")

        # STAGE 2: Self-Critique & Refinement Loop
        critique_prompt = (
            f"Review Draft:\n{stage1_clean}\n\n"
            f"Code Diff Reference:\n{diff_text}\n\n"
            "Act as a critical auditor. Review the draft suggestions and verify:\n"
            "1. Do the line numbers specified actually match lines added or modified in the diff (lines starting with '+' under the file)? "
            "If a line number points to code that is unmodified or deleted, correct the line number to the closest appropriate added line, or remove the comment entirely.\n"
            "2. Are there any false positives, hallucinated APIs, or nitpicks that violate coding standards? If so, remove them.\n"
            "3. Is the severity rating correct? (Only mark as 'CRITICAL' if it is a major bug, security vulnerability, or checklist failure).\n\n"
            "Output the final refined code review in the required JSON schema format."
        )

        logger.info("Stage 2: Requesting agentic self-critique and validation...")
        stage2_raw = call_llm_provider(provider, model, api_key, system_instruction, critique_prompt)
        stage2_clean = clean_json_string(stage2_raw)
        
        final_result = ReviewResult.parse_raw(stage2_clean)
        logger.info(f"Stage 2 self-critique completed. Retained {len(final_result.comments)} comments.")
        
        return final_result

    except Exception as e:
        logger.error(f"Error during LLM review generation: {e}")
        logger.error(traceback.format_exc())
        return None

def post_review_comments(mr, review_data: ReviewResult):
    """
    Posts line-by-line comments as discussions on the GitLab Merge Request.
    If inline placement fails (due to diff alignment issues), falls back to a list of comments inside the summary.
    """
    failed_inline_comments = []
    
    # 1. Fetch current diff references for anchoring comments
    diff_refs = getattr(mr, "diff_refs", {})
    if isinstance(diff_refs, dict):
        base_sha = diff_refs.get("base_sha")
        head_sha = diff_refs.get("head_sha")
        start_sha = diff_refs.get("start_sha")
    else:
        base_sha = getattr(diff_refs, "base_sha", None)
        head_sha = getattr(diff_refs, "head_sha", None)
        start_sha = getattr(diff_refs, "start_sha", None)

    can_post_inline = all([base_sha, head_sha, start_sha])
    
    if not can_post_inline:
        logger.warning("Missing MR diff SHAs. Falling back to posting all comments in the main thread.")
        failed_inline_comments.extend(review_data.comments)
    else:
        for comment in review_data.comments:
            if comment.line_number is None:
                logger.info(f"Comment on {comment.file_path} has no line number. Adding to fallback list.")
                failed_inline_comments.append(comment)
                continue
                
            body = (
                f"### 🤖 Antigravity AI Agent Review: {comment.severity}\n"
                f"**Issue:** {comment.issue_description}\n\n"
                f"**Suggested Fix:**\n{comment.suggestion}"
            )
            
            position = {
                "base_sha": base_sha,
                "head_sha": head_sha,
                "start_sha": start_sha,
                "new_path": comment.file_path,
                "new_line": comment.line_number,
                "position_type": "text"
            }
            
            try:
                # Create as a new MR Discussion (thread) at the specified position
                mr.discussions.create({
                    "body": body,
                    "position": position
                })
                logger.info(f"Posted inline comment on {comment.file_path}:{comment.line_number}")
            except Exception as e:
                logger.warning(
                    f"Failed to post inline comment on {comment.file_path}:{comment.line_number}. "
                    f"Error: {e}. Storing for fallback."
                )
                failed_inline_comments.append(comment)

    # 2. Construct Overall Summary Comment
    summary_body = f"## 🤖 Antigravity AI Agent Review Summary\n\n"
    summary_body += f"**Checklist Status:** {review_data.checklist_status}\n\n"
    summary_body += f"{review_data.summary}\n\n"
    
    # Add block decision info
    if review_data.should_approve:
        summary_body += "🟢 **Verdict:** All checklist checks passed successfully. No blocking issues found.\n\n"
    else:
        summary_body += f"🔴 **Verdict:** Review failed checklist criteria. Requires fixing `{settings.block_on_severity}` issues before merging.\n\n"

    # Append failed inline comments to the summary so developers don't lose them
    if failed_inline_comments:
        summary_body += "### 📝 Additional Review Comments\n"
        summary_body += "*(These comments couldn't be placed inline due to GitLab diff mapping limit:)*\n\n"
        for i, c in enumerate(failed_inline_comments, 1):
            summary_body += (
                f"#### {i}. `{c.file_path}` (Line {c.line_number}) - **{c.severity}**\n"
                f"- **Description:** {c.issue_description}\n"
                f"- **Suggestion:**\n{c.suggestion}\n\n"
                f"---\n"
            )

    try:
        # Post summary as a main comment on the MR
        mr.notes.create({"body": summary_body})
        logger.info("Posted overall MR review summary note.")
    except Exception as e:
        logger.error(f"Failed to post MR summary note: {e}")

    # 3. Handle GitLab MR Approvals
    # Checking block severity
    critical_comments = [c for c in review_data.comments if c.severity == "CRITICAL"]
    warning_comments = [c for c in review_data.comments if c.severity == "WARNING"]
    
    has_blocking_issue = False
    if settings.block_on_severity == "CRITICAL" and len(critical_comments) > 0:
        has_blocking_issue = True
    elif settings.block_on_severity == "WARNING" and (len(critical_comments) > 0 or len(warning_comments) > 0):
        has_blocking_issue = True

    try:
        # Note: Approve / Unapprove API requires GitLab premium or proper permissions.
        # If it fails, we catch the exception and proceed.
        if not has_blocking_issue and review_data.should_approve:
            mr.approve()
            logger.info("Successfully approved MR.")
        else:
            try:
                mr.unapprove()
                logger.info("Successfully unapproved MR (changes requested).")
            except Exception:
                # If unapprove is not supported or was not approved, ignore
                pass
    except Exception as e:
        logger.warning(f"GitLab approval action skipped / not supported on this repo tier: {e}")
