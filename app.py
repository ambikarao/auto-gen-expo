from fastapi import FastAPI, Request, Header
import requests, os
import re
import tempfile
import shutil
import base64
import logging
from openai import OpenAI
import git
from git import Repo
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = FastAPI()

# Initialize OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

AGENT_URL = os.getenv("AGENT_URL", "https://example-agent.com/handle-pr")

@app.get("/")
def health():
    return {"status": "ok"}

@app.post("/webhook")
async def github_webhook(request: Request, x_github_event: str = Header(None)):
    payload = await request.json()
    action = payload.get("action")
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})

    pr_details = {
        "action": action,
        "pr_number": pr.get("number"),
        "author": pr.get("user", {}).get("login"),
        "repo_name": repo.get("full_name"),
        "pr_title": pr.get("title"),
        "pr_url": pr.get("html_url"),
    }

    print("📬 Received PR Event:", pr_details)

    # Check if this is a PR event that should trigger fixing
    if action in ["opened", "synchronize"] and pr.get("number"):
        try:
            # Directly trigger the fixing process using PR details
            fix_result = await fix_pr_from_webhook(pr_details)
            print("✅ Auto-fix result:", fix_result)
        except Exception as e:
            print("❌ Error in auto-fix:", e)

    return {"message": "Webhook received"}

async def fix_pr_from_webhook(pr_details):
    pr_url = pr_details['pr_url']
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise Exception("GitHub token not configured")

    owner, repo, pr_number = parse_pr_url(pr_url)

    pr_data = get_pr_details(owner, repo, pr_number, token)
    head_sha = pr_data['head']['sha']
    head_branch = pr_data['head']['ref']

    files = get_pr_files(owner, repo, pr_number, token)

    file_updates = {}
    for file in files:
        if file['status'] in ['modified', 'added']:
            filename = file['filename']
            try:
                content = get_file_content(owner, repo, filename, head_sha, token)
                fixed_content = fix_code_with_ai(content, filename)
                file_updates[filename] = fixed_content
            except Exception as e:
                logger.error(f"Error processing {filename}: {str(e)}")
                continue

    if not file_updates:
        return {"message": "No files were updated"}

    clone_and_update_repo(owner, repo, head_branch, token, file_updates)

    return {
        "message": "PR updated successfully",
        "updated_files": list(file_updates.keys())
    }

def parse_pr_url(pr_url):
    """Parse GitHub PR URL to extract owner, repo, and PR number."""
    match = re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)', pr_url)
    if not match:
        raise ValueError("Invalid GitHub PR URL format")
    owner, repo, pr_number = match.groups()
    return owner, repo, int(pr_number)

def get_pr_details(owner, repo, pr_number, token):
    """Get PR details from GitHub API."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {"Authorization": f"token {token}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

def get_pr_files(owner, repo, pr_number, token):
    """Get list of files changed in the PR."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
    headers = {"Authorization": f"token {token}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

def get_file_content(owner, repo, path, ref, token):
    """Get file content from GitHub API."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {"Authorization": f"token {token}"}
    params = {"ref": ref}
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    content = response.json()['content']
    return base64.b64decode(content).decode('utf-8')

def fix_code_with_ai(code, filename):
    """Send code to OpenAI for fixing."""
    prompt = f"""The following code file '{filename}' is part of a pull request that has build failures.
Please analyze the code and fix any syntax errors, logical errors, or issues that could cause build failures.
Return only the complete corrected code without any explanations or markdown formatting.

Code:
{code}
"""
    try:
        response = client.chat.completions.create(
            model=os.getenv('OPENAI_MODEL', 'gpt-4o-mini'),
            messages=[
                {"role": "system", "content": "You are a code fixing assistant. Return only the corrected code."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=4000,
            temperature=0.1
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        raise Exception(f"OpenAI API error: {str(e)}")

def clone_and_update_repo(owner, repo, branch, token, file_updates):
    """Clone repo, update files, commit and push."""
    repo_url = f"https://github.com/{owner}/{repo}.git"
    auth_repo_url = f"https://{token}@github.com/{owner}/{repo}.git"

    temp_dir = tempfile.mkdtemp()
    try:
        # Clone the repo
        cloned_repo = Repo.clone_from(repo_url, temp_dir)

        # Checkout the branch
        cloned_repo.git.checkout(branch)

        # Update files
        for file_path, new_content in file_updates.items():
            full_path = os.path.join(temp_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(new_content)

        # Add, commit, and push
        cloned_repo.git.add(all=True)
        cloned_repo.index.commit("Fix build errors automatically")
        cloned_repo.git.push(auth_repo_url, branch)

    finally:
        # Close the repo to release file handles
        if 'cloned_repo' in locals():
            cloned_repo.close()

        # Clean up the temporary directory
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"Failed to clean up temp directory {temp_dir}: {e}")

@app.post("/fix-pr")
async def fix_pr(request: Request):
    try:
        data = await request.json()
        if not data or 'pr_url' not in data:
            return {"error": "Missing 'pr_url' in request body"}

        pr_url = data['pr_url']
        token = os.getenv("GITHUB_TOKEN")
        if not token:
            return {"error": "GitHub token not configured"}

        # Parse PR URL
        owner, repo, pr_number = parse_pr_url(pr_url)

        # Get PR details
        pr_details = get_pr_details(owner, repo, pr_number, token)
        head_sha = pr_details['head']['sha']
        head_branch = pr_details['head']['ref']

        # Get changed files
        files = get_pr_files(owner, repo, pr_number, token)

        file_updates = {}
        for file in files:
            if file['status'] in ['modified', 'added']:
                filename = file['filename']
                try:
                    content = get_file_content(owner, repo, filename, head_sha, token)
                    fixed_content = fix_code_with_ai(content, filename)
                    file_updates[filename] = fixed_content
                except Exception as e:
                    logger.error(f"Error processing {filename}: {str(e)}")
                    continue

        if not file_updates:
            return {"message": "No files were updated"}

        # Clone, update, commit, push
        clone_and_update_repo(owner, repo, head_branch, token, file_updates)

        return {
            "message": "PR updated successfully",
            "updated_files": list(file_updates.keys())
        }

    except ValueError as e:
        return {"error": str(e)}
    except requests.exceptions.RequestException as e:
        return {"error": f"GitHub API error: {str(e)}"}
    except Exception as e:
        return {"error": f"Internal error: {str(e)}"}
