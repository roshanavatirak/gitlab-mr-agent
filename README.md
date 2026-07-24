# 🤖 Standalone GitLab MR & Code Reviewer AI Agent

A production-grade, 100% standalone AI agent that automatically reviews GitLab Merge Requests (MRs) targeting branches like `dev` or `main`. It uses **Google's free Gemini 1.5 Flash API** to run multi-step code evaluations, uses **Upstash Redis** (free tier) as a robust background task broker, and runs inside a single Docker container.

## 🌟 Key Agent Features
- **FastAPI Webhook Handler**: Receives GitLab events, validates webhook authentication signatures, and acknowledges immediately to GitLab (< 50ms) to avoid timeouts.
- **Upstash Redis Broker**: Queues review jobs reliably so that heavy workloads are throttled and no events are lost.
- **Multi-Step Agentic Reasoning**: The agent drafts reviews, runs a **Self-Critique phase** to verify line mappings and eliminate false positives, and generates code suggestions.
- **GitLab Integration**: Posts inline discussions directly to the modified lines of code. If inline positioning fails, it automatically falls back to an appended index in the main MR thread.
- **Autonmous Decisions**: Automatically approves or unapproves MRs based on your severity settings (e.g., blocking merges on `CRITICAL` security violations).

---

## 🛠️ Step 1: Gather API Keys & Credentials (100% Card-Free)

1. **Google Gemini API Key** (Free, no credit card):
   - Go to [Google AI Studio](https://aistudio.google.com/).
   - Sign in with your Google Account.
   - Click **Create API Key** and copy the key.

2. **Upstash Redis connection string** (Free, no credit card):
   - Go to [Upstash Console](https://console.upstash.com/).
   - Sign in using GitHub or Google.
   - Click **Create Database**. Name it (e.g. `gitlab-agent-queue`), select a region close to you, and click **Create**.
   - Scroll down to the **Connection Details** section, copy the **Redis URL** (which starts with `rediss://...`).

3. **GitLab Access Token**:
   - In GitLab, go to your **User Settings** -> **Access Tokens** (or Project / Group Settings -> Access Tokens).
   - Create a token with a descriptive name, select the **api** scope, and click **Create personal access token**. Copy it immediately.

4. **Webhook Secret Token**:
   - Generate a secure random string (e.g., `openssl rand -hex 24` or any random password). You will put this in both the GitLab Webhook settings and the agent's environment variables to secure the endpoint.

---

## 🚀 Step 2: Deployment Options

### Option A: Back4app Containers (Recommended Cloud Hosting - 100% Free & No Card)

Back4app Containers allows you to host Docker containers for free without credit card verification:

1. **Upload Code to GitHub**:
   - Go to **[GitHub](https://github.com/)** and create a new **Private** repository (e.g., `gitlab-mr-agent`).
   - In your local terminal, push the agent code to your new GitHub repository:
     ```bash
     cd gitlab-mr-reviewer
     git init
     git add .
     git commit -m "deploy agent"
     git branch -M main
     git remote add origin https://github.com/YOUR_USERNAME/gitlab-mr-agent.git
     git push -u origin main
     ```
2. **Create Back4app Account**:
   - Go to **[back4app.com](https://www.back4app.com/)** and sign up for a free account (no credit card required).
3. **Deploy the Container**:
   - In the Back4app dashboard, click **Build new app** -> **Containers (Web App)**.
   - Connect your GitHub account and authorize Back4app to read your `gitlab-mr-agent` repository.
   - Select your repository and click **Deploy**.
4. **Configure Environment Variables (Secrets)**:
   - In your Back4app App Dashboard, go to **App Settings** -> **Environment Variables**.
   - Click **Add** to add the following variables one by one:
     - `UPSTASH_REDIS_URL`: `rediss://default:gQAAAAAAArOrAAIgcDIzZTI0NDQ4MjkzN2Q0OTlhODA1NzdkOTZlMmQ5MzA1NQ@helping-satyr-177067.upstash.io:6379`
     - `GITLAB_URL`: `https://gitlab.com` *(or your company's GitLab instance)*
     - `GITLAB_TOKEN`: `glpat-enYRKWLRR56htm6sWhc27286MQp1OmwH.01.0w1vmk5y7`
     - `WEBHOOK_SECRET`: `your_webhook_secret_key` *(a random password of your choice)*
     - `GEMINI_API_KEY`: `AQ.Ab8RN6J4ZXiKKrHtFc6wyVRpqaHlHGZZQFSeZeJElW7kmfIZ6g`
     - `TARGET_BRANCHES`: `dev,main`
     - `BLOCK_ON_SEVERITY`: `CRITICAL`
     - `REVIEW_CHECKLIST`: *(Provide custom checklist guidelines)*
   - Back4app will automatically rebuild and redeploy the container with these environment variables.
5. **Get Your Public HTTPS URL**:
   - Once deployment completes, Back4app will display your public URL at the top of the app dashboard (e.g., `https://gitlab-mr-agent-xxxx.b4aspaces.com`). Your webhook target endpoint will be `https://gitlab-mr-agent-xxxx.b4aspaces.com/webhook`.

---

### Option B: Local Office/Home Server via Cloudflare Tunnel (100% Free & No Card)

If you prefer hosting the agent on an office or home server, use a Cloudflare Tunnel to expose it securely without port forwarding:

1. Install Docker on your server.
2. Create a folder `gitlab-mr-reviewer` and copy all project files (`main.py`, `worker.py`, `requirements.txt`, `Dockerfile`, `start.sh`, and `.env`).
3. Fill in the values in the `.env` file.
4. Run the Docker container:
   ```bash
   docker build -t gitlab-mr-agent .
   docker run -d --name mr-agent -p 7860:7860 --env-file .env gitlab-mr-agent
   ```
5. Install and configure **Cloudflare Tunnel** (Requires a free Cloudflare account):
   - Set up a Cloudflare Tunnel on your Cloudflare Dashboard (Access -> Tunnels).
   - Install the `cloudflared` daemon on your server using the commands provided.
   - Route traffic from a public hostname (e.g., `mr-agent.yourdomain.com`) to `http://localhost:7860` on the server.
   - The agent is now exposed securely with auto-renewing HTTPS.

---

## 🔗 Step 3: Configure GitLab Webhooks

Once your agent is deployed and you have a secure HTTPS URL:

1. Go to your **GitLab Project** (or Group settings for all repositories).
2. Navigate to **Settings** -> **Webhooks**.
3. Fill in the details:
   - **URL**: `https://<your-agent-url>/webhook` (e.g. `https://john-gitlab-agent.hf.space/webhook`).
   - **Secret Token**: Paste the `WEBHOOK_SECRET` value.
   - **Triggers**: Check **Merge request events**.
   - **SSL verification**: Ensure **Enable SSL verification** is checked.
4. Click **Add webhook**.
5. Test the webhook by clicking **Test** -> **Merge requests events**. It should return an HTTP status code `200`.

---

## 🧪 Step 4: Test the AI Agent Reviewer

1. Create a new branch (e.g., `feature/test-review`) on your GitLab project.
2. Modify a file to intentionally introduce a potential bug (e.g. write an insecure SQL query or hardcode an API secret).
3. Commit and push:
   ```bash
   git add .
   git commit -m "insecure commit test"
   git push origin feature/test-review
   ```
4. Open a **Merge Request** targeting the `dev` branch.
5. In a few seconds, the GitLab webhook fires, the FastAPI server enqueues the job into Upstash Redis, and the background worker processes the review.
6. The AI Agent will post inline comments on the insecure lines and create a summary card in the main MR comment thread!
