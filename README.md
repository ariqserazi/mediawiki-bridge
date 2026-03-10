![MediaWiki Bridge Logo](assets/owl.png)
# MediaWiki Bridge

Canonical infrastructure for lore accurate LLM systems

This project provides a FastAPI based bridge service that allows a custom LLM tool to pull canon text directly from MediaWiki powered wikis with strict source control and citation discipline.

It is built for

* Long form lore analysis
* Video essay scripting
* Canon verification
* Franchise accurate writing workflows

The goal is a Lore LLM that can explain fictional worlds while citing the exact wiki pages used.

---

# What this project does

You are building a small pipeline composed of four parts.

## 1. MediaWiki Bridge API

A public HTTPS FastAPI service that an LLM can call.

This is the core component.
It enables lookups inside an LLM with the ability to connect to an MCP server.

## 2. MediaWiki MCP Server

An optional local tool server used for development and testing.

This is not required for end users once the Bridge API is deployed publicly.

## 3. Webhost deployment

Hosts the Bridge API and provides a stable HTTPS URL.

Any webhost that supports container or Python deployment can be used.

## 4. LLM Tool Integration

Connects the LLM to the Bridge API using an OpenAPI schema.

In production usage only the Bridge API and the tool integration are required.
Users do not need Docker or the MCP server.

---

# Supported sources

Only canon friendly MediaWiki sources are allowed.

* fandom.com
* wiki.gg
* wikipedia may be used as a fallback source

---

# Requirements

Required

* Docker Desktop
* Git
* A webhost account
* An LLM platform that supports tool or action integration

Optional if running without Docker

* Python 3.11 or newer

---

# Repository layout

Recommended project structure

.
├── app.py
├── requirements.txt
├── Dockerfile
├── openapi.yaml
└── README.md

If your main file is named differently adjust commands accordingly.

---

# Environment variables

All environment variables are optional.

USER_AGENT
Default value is a MediaWiki bridge version string

HTTP_TIMEOUT
Default value is 30.0 seconds

No API keys are required.

---

# Running locally without Docker

Install dependencies

pip install fastapi uvicorn httpx

Run the server

uvicorn app:app --host 0.0.0.0 --port 8000

Open in your browser

http://localhost:8000/health
http://localhost:8000/docs

---

# Running the Bridge with Docker

## Dockerfile example

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

ENV PORT=8000
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

If your Python file is named main.py change app:app to main:app.

## requirements.txt example

fastapi==0.115.0
uvicorn[standard]==0.30.6
httpx==0.27.2

## Build and run the container

docker build -t mediawiki-bridge .

docker run -p 8000:8000 --name mediawiki-bridge mediawiki-bridge

Test the service

http://localhost:8000/health

---

# Common API test calls

Resolve a topic to a working wiki base

http://localhost:8000/resolve?topic=Devil%20May%20Cry%205

Search within a resolved wiki

http://localhost:8000/search?q=Vergil&wiki=https%3A%2F%2Fdevilmaycry.fandom.com

Fetch a page extract and citeable URL

http://localhost:8000/page?title=Vergil&wiki=https%3A%2F%2Fdevilmaycry.fandom.com

Best practice

Always call resolve first then pass the returned wiki URL into search and page.

---

# MediaWiki MCP Server

This step is optional and intended for local development only.
LLM tools do not communicate with MCP directly.

Clone the MCP server repository

git clone https://github.com/shiquda/mediawiki-mcp-server
cd mediawiki-mcp-server

Build the image

docker build -t mediawiki-mcp-server .

Run the server

docker run --rm -p 8080:8080 --name mediawiki-mcp-server mediawiki-mcp-server

---

# Security notes

Outbound requests are restricted by hostname suffix.

Allowed domains

* fandom.com
* wiki.gg

This prevents the service from being abused as a general proxy.

---

# License

MIT

---

# Privacy Policy

This service does not collect or store personal user data.

Requests are used only to retrieve publicly available information from MediaWiki APIs.

Client IP addresses may be temporarily processed for request handling diagnostics and abuse prevention but are not stored persistently or used for tracking.

Abusive traffic may be blocked using temporary IP filtering.

No cookies analytics or tracking technologies are used.