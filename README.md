# 📚 MediaWiki Bridge :D

✨ Canon first infrastructure for lore accurate GPTs ✨

This project provides a FastAPI based bridge service that allows a Custom GPT Action to pull **canon text directly from MediaWiki powered wikis**, with strict source control and citation discipline.

It is built for:

* Long form lore analysis  
* Video essay scripting  
* Canon verification  
* Franchise accurate writing workflows  

The end goal is a **Lore GPT** that can explain fictional worlds while always citing the exact wiki pages it used.

---

## 🧠 What this project does

You are building a small but powerful pipeline composed of four parts.

### 🔹 1. MediaWiki Bridge API
A public HTTPS FastAPI service that ChatGPT Actions can call.

This is the core component.  
It enables canon lookups inside a Custom GPT.

### 🔹 2. MediaWiki MCP Server
An optional local tool server for development and testing.

This is not required for end users once the Bridge API is deployed publicly.

### 🔹 3. Render deployment
Hosts the Bridge API and provides a stable HTTPS URL required by GPT Actions.

### 🔹 4. GPT Action
Connects ChatGPT to the Bridge API using an OpenAPI schema.

✅ In production usage, only the Bridge API and GPT Action are required.  
Friends and users do not need Docker or the MCP server.

---

## 🌍 Supported sources

Only canon friendly MediaWiki sources are allowed.

* fandom.com  
* wiki.gg  

🚫 Wikipedia is intentionally excluded.

---

## 🧰 Requirements

Required:

* Docker Desktop  
* Git  
* A Render account  
* A ChatGPT account with Custom GPT Actions enabled  

Optional if running without Docker:

* Python 3.11 or newer  

---

## 📂 Repository layout

Recommended project structure:

```text
.
├── app.py            # FastAPI service code
├── requirements.txt  # Python dependencies
├── Dockerfile        # Container build
├── openapi.yaml      # GPT Action schema
└── README.md
```

If your main file is named differently, adjust commands accordingly.

---

## ⚙️ Environment variables

All environment variables are optional.

* `USER_AGENT`  
  Default value is a mediawiki bridge version string

* `HTTP_TIMEOUT`  
  Default value is 30.0 seconds

🔐 No API keys are required.

---

## 🚀 Running locally without Docker

Install dependencies:

```bash
pip install fastapi uvicorn httpx
```

Run the server:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open in your browser:

```text
http://localhost:8000/health
http://localhost:8000/docs
```

---

## 🐳 Running the Bridge with Docker

### 📄 Dockerfile example

Create a file named `Dockerfile`:

```Dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

ENV PORT=8000
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

If your Python file is named `main.py`, change `app:app` to `main:app`.

---

### 📦 requirements.txt example

```text
fastapi==0.115.0
uvicorn[standard]==0.30.6
httpx==0.27.2
```

---

### 🏗️ Build and run the container

Build the image:

```bash
docker build -t mediawiki-bridge .
```

Run the container:

```bash
docker run -p 8000:8000 --name mediawiki-bridge mediawiki-bridge
```

Test the service:

```text
http://localhost:8000/health
```

---

## 🔍 Common API test calls

Resolve a topic to a working wiki base:

```text
http://localhost:8000/resolve?topic=Devil%20May%20Cry%205
```

Search within a resolved wiki:

```text
http://localhost:8000/search?q=Vergil&wiki=https%3A%2F%2Fdevilmaycry.fandom.com
```

Fetch a page extract and citeable URL:

```text
http://localhost:8000/page?title=Vergil&wiki=https%3A%2F%2Fdevilmaycry.fandom.com
```

💡 Best practice  
Always call resolve first, then pass the returned wiki URL into `search` and `page`.

---

## 🧪 MediaWiki MCP Server

This step is optional and intended for local development only.  
GPT Actions do not talk to MCP directly.

Clone the MCP server repository:

```bash
git clone https://github.com/shiquda/mediawiki-mcp-server
cd mediawiki-mcp-server
```

Build the image:

```bash
docker build -t mediawiki-mcp-server .
```

Run the server:

```bash
docker run --rm -p 8080:8080 --name mediawiki-mcp-server mediawiki-mcp-server
```

---

## ☁️ Deploying the Bridge to Render

Render provides the public HTTPS endpoint required by GPT Actions.

### 📌 Deploy from GitHub

1. Push your bridge code to a GitHub repository  
2. In Render, create a new Web Service  
3. Connect the GitHub repository  
4. Select Docker as the environment  

### ⚙️ Render settings

* Build Command  
  Leave empty when using Docker

* Start Command  
  Leave empty when using Docker

* Port  
  Automatically mapped by Render

* Environment variables  
  Optional `USER_AGENT`, `HTTP_TIMEOUT`

---

### ✅ After deployment

Your service URL will look like:

```text
https://your-service-name.onrender.com
```

Verify:

```text
https://your-service-name.onrender.com/health
https://your-service-name.onrender.com/openapi.json
https://your-service-name.onrender.com/docs
```

If `/health` returns ok, the service is live.

---

## 🤖 Adding the GPT Action in ChatGPT

### Step 1

* Open ChatGPT  
* Go to Explore GPTs  
* Create a GPT  
* Open Configure  
* Navigate to Actions  
* Click Add Action  
* Choose Import from OpenAPI  

### Step 2

Paste or upload your OpenAPI schema.

Ensure the server URL matches your Render deployment:

```yaml
servers:
  - url: https://your-service-name.onrender.com
```

### Step 3

Test the Action inside the GPT builder.

Recommended call order:

* resolveWiki  
* searchWiki  
* getWikiPage  

---

## 🧭 Recommended Lore GPT behavior

Suggested instruction logic for your Custom GPT:

* Resolve the franchise topic  
* Search the resolved wiki for relevant pages  
* Fetch page extracts and URLs  
* Cite every factual claim using returned URLs  
* Use only Fandom and wiki.gg  
* Explicitly say when canon cannot be verified  

---

## 🛠️ Troubleshooting

### Resolve returns could not resolve topic

This usually means the wiki site name differs from the user input.

Example:

* Devil May Cry 5  
* devilmaycry.fandom.com  

Fixes:

* Try a shorter franchise name  
* Inspect the `tried` list returned by resolve  

---

### Works in docs but fails in GPT Actions

Common causes:

* OpenAPI schema mismatch  
* Schema not refreshed after code changes  
* Older schema imported by another user  

Fix:

* Open `/openapi.json` from the deployed service  
* Reimport the schema in the GPT Action  

---

### Fandom quirks

Some Fandom sites behave inconsistently.

If site probing fails but search works, update your probe logic to use a minimal search query instead.

---

### CORS issues

GPT Actions call your service server to server.  
CORS is usually not required unless you also build a browser based frontend.

---

## 🔐 Security notes

Outbound requests are restricted by hostname suffix.

Allowed domains:

* fandom.com  
* wiki.gg  

This prevents the service from being abused as a general purpose proxy.

---

## 📜 License

Choose a license for your repository.

Recommended:

* MIT


## Privacy Policy

This service does not collect, store, or share personal data.

Requests are used only to retrieve publicly available information from MediaWiki APIs.
No user identifiers, IP addresses, or request contents are persisted.
No cookies, analytics, or tracking technologies are used.
