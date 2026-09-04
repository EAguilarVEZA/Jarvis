"""
workflows_api
─────────────
Agent Studio — orchestrate the agent library into TEAMS that work in SEQUENCE.
A workflow is an ordered list of steps; each step assigns an agent a task, and
each agent receives the outputs of the steps before it as context (so agent 2
builds on agent 1's work). Agents can run real analyses via their tools, so a
workflow can, e.g.: audit paid media → design new creative → write the email →
verify tracking — end to end, then be scheduled.

Routes (prefix /api/workflows):
  GET    /api/workflows                 — list saved workflows
  POST   /api/workflows                 — create/update a workflow
  GET    /api/workflows/{id}            — one workflow
  DELETE /api/workflows/{id}
  POST   /api/workflows/{id}/run        — run a saved workflow (sequential)
  POST   /api/workflows/run_adhoc       — run an unsaved steps array
  POST   /api/workflows/suggest         — AI proposes a sequence for a goal
  GET    /api/workflows/templates       — built-in marketing sequences
  POST   /api/workflows/{id}/schedule   — set a cron schedule
  GET    /api/workflows/runs            — recent run history
"""
from __future__ import annotations

import system_llm  # route LLM calls through the active system model
import json
import os
import re
import time
import uuid
import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/workflows", tags=["workflows"])
log = logging.getLogger("workflows_api")

try:
    import crypto_store  # encryption at rest for credentials + secrets
except Exception:  # pragma: no cover
    crypto_store = None

_WF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows.json")
_RUN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflow_runs.json")
_CRED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflow_credentials.json")
_MCP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_servers.json")
_LLM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_providers.json")


def _get_llm_provider(pid):
    for p in _load(_LLM_PATH, "providers").get("providers", []):
        if p.get("id") == pid:
            # A provider fetched by explicit id (per-node model choice, Test button)
            # is honored as-is and never overridden by the global system model.
            return {**p, "_explicit": True}
    return None
_VERS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflow_versions.json")
_APPROVAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflow_approvals.json")


def _snapshot_version(prev):
    """Store a snapshot of a workflow record before it's overwritten (bounded to 20)."""
    if not prev or not prev.get("id"):
        return
    store = _load(_VERS_PATH, "versions")
    allv = store.get("versions")
    if not isinstance(allv, dict):
        allv = {}
    store["versions"] = allv
    lst = allv.setdefault(prev["id"], [])
    lst.insert(0, {"vid": uuid.uuid4().hex[:10], "at": int(time.time()),
                   "name": prev.get("name"),
                   "snapshot": {k: prev.get(k) for k in ("name", "context", "recipients",
                                "nodes", "edges", "notes", "steps", "alert_webhook", "error_workflow_id", "schedule")}})
    del lst[20:]
    _save(_VERS_PATH, store)

# Credential type catalogue — what secret fields each connector needs.
_CRED_TYPES = {
    "slack_webhook":   {"label": "Slack incoming webhook",   "fields": ["url"]},
    "teams_webhook":   {"label": "Teams incoming webhook",   "fields": ["url"]},
    "discord_webhook": {"label": "Discord webhook",          "fields": ["url"]},
    "airtable_pat":    {"label": "Airtable token",           "fields": ["token"]},
    "google_oauth":    {"label": "Google OAuth token",       "fields": ["token"]},
    "notion_token":    {"label": "Notion integration token", "fields": ["token"]},
    "hubspot_token":   {"label": "HubSpot private-app token","fields": ["token"]},
    "stripe_key":      {"label": "Stripe secret key",        "fields": ["token"]},
    "github_token":    {"label": "GitHub token",             "fields": ["token"]},
    "bearer":          {"label": "Bearer token (any API)",   "fields": ["token"]},
    "postgres":        {"label": "Postgres database",
                        "fields": ["host", "port", "database", "user", "password"]},
}


def _load(path, key):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {key: []}


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ── Built-in marketing team templates (agent slugs must exist in the library) ──
_TEMPLATES = [
    {"id": "tpl-fullfunnel", "name": "Full-funnel campaign launch",
     "description": "Plan, buy, create, capture, and measure a new campaign end-to-end.",
     "steps": [
         {"agent_slug": "growth-hacker", "task": "Propose the highest-leverage campaign to launch this quarter and the target segment. Be specific."},
         {"agent_slug": "paid-media-auditor", "task": "Audit current paid media for waste and headroom that this campaign should exploit."},
         {"agent_slug": "ad-creative-strategist", "task": "Design the ad creative concepts and hooks for the campaign above."},
         {"agent_slug": "email-marketing-strategist", "task": "Design the nurture email sequence that converts the traffic this campaign drives."},
         {"agent_slug": "tracking-measurement-specialist", "task": "Specify the tracking + measurement plan so we can prove impact in Test & Learn."},
     ]},
    {"id": "tpl-seo-content", "name": "SEO + content growth sprint",
     "description": "From keyword strategy to content to distribution.",
     "steps": [
         {"agent_slug": "seo-specialist", "task": "Identify the top keyword opportunities and content gaps to target."},
         {"agent_slug": "content-creator", "task": "Turn those opportunities into a concrete content plan with titles and angles."},
         {"agent_slug": "social-media-strategist", "task": "Plan how to distribute and amplify this content across channels."},
     ]},
    {"id": "tpl-explain-drop", "name": "Diagnose a metric drop",
     "description": "Investigate why a KPI moved, then plan the fix.",
     "steps": [
         {"agent_slug": "growth-hacker", "task": "Use explain_metric / driver_analysis on the metric the user names to find what drove the change. Report the drivers."},
         {"agent_slug": "paid-social-strategist", "task": "Given those drivers, recommend concrete paid-social actions to recover or accelerate."},
     ]},
    {"id": "tpl-launch-week", "name": "Product launch week",
     "description": "PR, social, and email working together for a launch.",
     "steps": [
         {"agent_slug": "pr-communications-manager", "task": "Draft the launch narrative and press angle."},
         {"agent_slug": "linkedin-content-creator", "task": "Turn the narrative into a LinkedIn launch content series."},
         {"agent_slug": "email-marketing-strategist", "task": "Write the launch email campaign to the list."},
     ]},
]


# Ready-made node graphs showcasing the flow/data/connector nodes.
def _gt_node(nid, typ, title, x, y, **params):
    n = {"id": nid, "type": typ, "title": title, "x": x, "y": y, "params": params}
    return n


_GRAPH_TEMPLATES = [
    {"id": "g-csv-slack", "name": "CSV → filter → Slack count",
     "description": "Parse a CSV, keep only paid rows with Code, count them, and post to Slack.",
     "graph": {
        "nodes": [
            _gt_node("n1", "manualTrigger", "Start", 60, 40),
            _gt_node("n2", "set", "Sample CSV", 60, 170, value="plan,email\npaid,a@co\nfree,b@co\npaid,c@co\n"),
            _gt_node("n3", "extractFile", "Parse CSV", 60, 300, format="csv"),
            _gt_node("n4", "code", "Keep paid", 60, 430, code="result=[i for i in items if i.get('plan')=='paid']"),
            _gt_node("n5", "aggregate", "Count", 60, 560, field="email", op="count"),
            _gt_node("n6", "set", "Message", 60, 690, value="Paid signups: {{$json.email}}"),
            _gt_node("n7", "slack", "Post to Slack", 60, 820, message="{{upstream}}"),
        ],
        "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}, {"from": "n3", "to": "n4"},
                  {"from": "n4", "to": "n5"}, {"from": "n5", "to": "n6"}, {"from": "n6", "to": "n7"}]}},
    {"id": "g-scrape-summarize", "name": "Fetch page → summarize → email",
     "description": "HTTP GET a URL, strip the HTML to text, summarize with AI, and email it.",
     "graph": {
        "nodes": [
            _gt_node("n1", "manualTrigger", "Start", 60, 40),
            {"id": "n2", "type": "http", "title": "Fetch page", "x": 60, "y": 170,
             "url": "https://example.com", "params": {"method": "GET", "url": "https://example.com"}},
            _gt_node("n3", "html", "To text", 60, 300, mode="text"),
            _gt_node("n4", "transform", "Summarize", 60, 430, instruction="Summarize this page in 3 bullet points."),
            {"id": "n5", "type": "email", "title": "Email me", "x": 60, "y": 560, "recipients": "", "params": {}},
        ],
        "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}, {"from": "n3", "to": "n4"},
                  {"from": "n4", "to": "n5"}]}},
    {"id": "g-loop-enrich", "name": "Loop items → enrich → collect",
     "description": "Split a JSON list, loop over each item enriching it with Code, then aggregate the results.",
     "graph": {
        "nodes": [
            _gt_node("n1", "manualTrigger", "Start", 60, 40),
            _gt_node("n2", "set", "Sample JSON", 60, 170, value='[{"name":"Ada"},{"name":"Bo"}]'),
            _gt_node("n3", "extractFile", "Parse JSON", 60, 300, format="json"),
            _gt_node("n4", "loop", "Loop", 60, 430),
            _gt_node("n5", "code", "Greet", 320, 560, code="result=[{'greeting':'Hi '+items[0].get('name','')}]"),
            _gt_node("n6", "aggregate", "Collect", 60, 690, field="greeting", op="list"),
        ],
        "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}, {"from": "n3", "to": "n4"},
                  {"from": "n4", "to": "n5", "branch": "loop"}, {"from": "n4", "to": "n6", "branch": "done"}]}},
    {"id": "g-webhook-branch", "name": "Webhook → branch by amount → respond",
     "description": "Receive a webhook, branch on the amount with IF, and respond differently.",
     "graph": {
        "nodes": [
            _gt_node("n1", "webhookTrigger", "Webhook", 60, 40),
            _gt_node("n2", "if", "Amount > 100?", 60, 170, left="{{$json.amount}}", op="gt", right="100"),
            _gt_node("n3", "set", "Big", 320, 300, value="High-value: {{$json.amount}}"),
            _gt_node("n4", "set", "Small", 60, 300, value="Standard: {{$json.amount}}"),
            _gt_node("n5", "respond", "Respond", 190, 430, value="{{upstream}}"),
        ],
        "edges": [{"from": "n1", "to": "n2"},
                  {"from": "n2", "to": "n3", "branch": "true"}, {"from": "n2", "to": "n4", "branch": "false"},
                  {"from": "n3", "to": "n5"}, {"from": "n4", "to": "n5"}]}},
]


# ── Marketing automation flow library (loads onto the canvas when clicked) ──
def _mn(nid, typ, title, y, x=60, **p):
    return {"id": nid, "type": typ, "title": title, "x": x, "y": y, "params": p}


def _ma(nid, slug, title, y, task, x=60):
    return {"id": nid, "type": "agent", "title": title, "x": x, "y": y,
            "agent_slug": slug, "task": task, "params": {}}


def _lin(tid, name, desc, nodes):
    """Build a template whose nodes are wired top-to-bottom in order."""
    edges = [{"from": nodes[i]["id"], "to": nodes[i + 1]["id"]} for i in range(len(nodes) - 1)]
    return {"id": tid, "name": name, "description": desc, "graph": {"nodes": nodes, "edges": edges}}


def _yc(i):  # vertical slot -> y coordinate
    return 40 + i * 128


_MKT_TEMPLATES = [
    _lin("m-lead-welcome", "Lead capture → CRM → welcome email",
         "New form lead is created in HubSpot and gets an instant branded welcome email.",
         [_mn("a", "webhookTrigger", "New lead (webhook)", _yc(0)),
          _mn("b", "set", "Normalize fields", _yc(1), value='{"email":"{{$json.email}}","name":"{{$json.name}}"}'),
          _mn("c", "hubspot", "Create contact", _yc(2), operation="create_contact"),
          _ma("d", "email-marketing-strategist", "Write welcome", _yc(3), "Write a warm 120-word welcome email for this new lead."),
          _mn("e", "gmail", "Send welcome", _yc(4), operation="send")]),
    _lin("m-signup-slack", "New signup → segment → Slack alert",
         "Route new signups by plan and alert the growth channel in Slack.",
         [_mn("a", "webhookTrigger", "New signup", _yc(0)),
          _mn("b", "if", "Paid plan?", _yc(1), left="{{$json.plan}}", op="eq", right="paid"),
          _mn("c", "slack", "Alert #wins", _yc(2), x=320, message="New PAID signup: {{$json.email}}"),
          _mn("d", "hubspot", "Tag free trial", _yc(2), operation="create_contact"),
          _mn("e", "slack", "Alert #signups", _yc(3), message="New signup: {{$json.email}}")],
         ),
    _lin("m-abandoned-cart", "Abandoned cart win-back",
         "Find carts with no purchase, write a personalized nudge, and email it.",
         [_mn("a", "manualTrigger", "Every 6 hours", _yc(0)),
          _mn("b", "gsheets", "Read carts", _yc(1), operation="append"),
          _mn("c", "filter", "No purchase", _yc(2), left="{{$json.purchased}}", op="eq", right="false"),
          _ma("d", "email-marketing-strategist", "Write win-back", _yc(3), "Write a friendly cart-recovery email with one incentive."),
          _mn("e", "gmail", "Send nudge", _yc(4), operation="send")]),
    _lin("m-content-calendar", "Weekly content calendar",
         "Plan a week of content and save the calendar to Notion.",
         [_mn("a", "manualTrigger", "Every Monday", _yc(0)),
          _ma("b", "content-creator", "Draft topics", _yc(1), "Propose 5 content pieces for the week with titles and angles."),
          _ma("c", "social-media-strategist", "Channel plan", _yc(2), "Turn those into a per-channel posting schedule for the week."),
          _mn("d", "notion", "Save calendar", _yc(3), operation="create_page")]),
    _lin("m-seo-sprint", "SEO keyword sprint → brief → doc",
         "Find keyword gaps, turn them into a content brief, and store it in Drive.",
         [_mn("a", "manualTrigger", "Start", _yc(0)),
          _ma("b", "seo-specialist", "Keyword gaps", _yc(1), "Identify the top 10 keyword opportunities and content gaps."),
          _ma("c", "content-creator", "Write briefs", _yc(2), "Turn the top opportunities into detailed content briefs."),
          _mn("d", "gdrive", "Save to Drive", _yc(3), operation="list_files")]),
    _lin("m-paid-audit", "Paid media waste audit → Slack",
         "Pull ad spend, audit for waste, and post the digest to Slack.",
         [_mn("a", "manualTrigger", "Every morning", _yc(0)),
          _mn("b", "http", "Fetch ad data", _yc(1), method="GET", url="https://api.example.com/ads/spend"),
          _ma("c", "paid-media-auditor", "Audit waste", _yc(2), "Audit this spend for wasted budget and headroom. List the top fixes."),
          _mn("d", "slack", "Post digest", _yc(3), message="{{upstream}}")]),
    {"id": "m-lead-scoring", "name": "Lead scoring → route by tier",
     "description": "Score inbound leads and route hot/warm/cold to the right place.",
     "graph": {"nodes": [
         _mn("a", "webhookTrigger", "New lead", _yc(0)),
         _mn("b", "code", "Score lead", _yc(1), code="for i in items:\n    i['score']=len(i.get('email',''))\nresult=items"),
         _mn("c", "switch", "Tier?", _yc(2), value="{{$json.score}}", cases="hot,warm,cold"),
         _mn("d", "slack", "Alert sales (hot)", _yc(3), x=340, message="HOT lead: {{$json.email}}"),
         _mn("e", "hubspot", "Nurture (warm)", _yc(3), x=60, operation="create_contact"),
         _mn("f", "gsheets", "Log (cold)", _yc(3), x=-220, operation="append")],
      "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"},
                {"from": "c", "to": "d", "branch": "hot"}, {"from": "c", "to": "e", "branch": "warm"},
                {"from": "c", "to": "f", "branch": "cold"}]}},
    _lin("m-newsletter", "Newsletter build & send",
         "Draft the issue, polish the email, and send to the list.",
         [_mn("a", "manualTrigger", "Start", _yc(0)),
          _ma("b", "content-creator", "Draft issue", _yc(1), "Draft this week's newsletter: 3 sections with short, useful copy."),
          _ma("c", "email-marketing-strategist", "Polish + subject", _yc(2), "Polish into a sendable email with 3 subject-line options."),
          _mn("d", "gmail", "Send newsletter", _yc(3), operation="send")]),
    _lin("m-review-request", "Post-purchase review request",
         "After a Stripe payment, wait a few days then ask for a review.",
         [_mn("a", "webhookTrigger", "Payment (Stripe)", _yc(0)),
          _mn("b", "wait", "Wait 3 days", _yc(1), seconds="259200"),
          _ma("c", "email-marketing-strategist", "Write ask", _yc(2), "Write a short, sincere review-request email."),
          _mn("d", "gmail", "Send request", _yc(3), operation="send")]),
    _lin("m-webinar-followup", "Webinar follow-up nurture",
         "Registrants flow into the CRM and receive a 3-touch nurture.",
         [_mn("a", "formTrigger", "Webinar signup", _yc(0)),
          _mn("b", "hubspot", "Add to CRM", _yc(1), operation="create_contact"),
          _ma("c", "email-marketing-strategist", "Design nurture", _yc(2), "Design a 3-email post-webinar nurture that drives a demo."),
          _mn("d", "gmail", "Send touch 1", _yc(3), operation="send")]),
    _lin("m-influencer-outreach", "Influencer outreach pipeline",
         "Loop a prospect list, personalize each pitch, and email it.",
         [_mn("a", "manualTrigger", "Start", _yc(0)),
          _mn("b", "gsheets", "Read prospects", _yc(1), operation="append"),
          _mn("c", "loop", "Per influencer", _yc(2)),
          _ma("d", "pr-communications-manager", "Personalize", _yc(3), x=340, task="Write a personalized outreach note for this influencer."),
          _mn("e", "gmail", "Send pitch", _yc(4), operation="send")],
         ),
    {"id": "m-ad-refresh", "name": "Ad creative refresh + approval",
     "description": "Generate fresh ad concepts, get human sign-off, then notify the team.",
     "graph": {"nodes": [
         _mn("a", "manualTrigger", "Weekly", _yc(0)),
         _ma("b", "ad-creative-strategist", "New concepts", _yc(1), "Propose 5 new ad creative concepts and hooks."),
         _mn("c", "approval", "Approve creative?", _yc(2)),
         _mn("d", "slack", "Ship to team", _yc(3), x=320, message="Approved creative: {{upstream}}"),
         _mn("e", "gmail", "Send revisions", _yc(3), x=60, operation="send")],
      "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"},
                {"from": "c", "to": "d", "branch": "approved"}, {"from": "c", "to": "e", "branch": "rejected"}]}},
    _lin("m-blog-repurpose", "Blog post → repurpose to social",
         "Fetch a new blog post, turn it into a LinkedIn series, and save to Notion.",
         [_mn("a", "manualTrigger", "New post", _yc(0)),
          _mn("b", "http", "Fetch post", _yc(1), method="GET", url="https://blog.example.com/latest"),
          _mn("c", "html", "To text", _yc(2), mode="text"),
          _ma("d", "linkedin-content-creator", "Make series", _yc(3), "Turn this post into a 5-part LinkedIn content series."),
          _mn("e", "notion", "Save drafts", _yc(4), operation="create_page")]),
    _lin("m-churn-winback", "Churn-risk win-back",
         "Score churn risk, filter the at-risk segment, and send a save offer.",
         [_mn("a", "manualTrigger", "Weekly", _yc(0)),
          _mn("b", "gsheets", "Read customers", _yc(1), operation="append"),
          _mn("c", "filter", "At-risk only", _yc(2), left="{{$json.risk}}", op="gt", right="0.6"),
          _ma("d", "email-marketing-strategist", "Save offer", _yc(3), "Write a retention email with a compelling save offer."),
          _mn("e", "gmail", "Send offer", _yc(4), operation="send")]),
    _lin("m-event-reg", "Event registration → calendar + confirm",
         "Registrant is added to the calendar and gets a confirmation email.",
         [_mn("a", "formTrigger", "Event signup", _yc(0)),
          _mn("b", "gcalendar", "Add to calendar", _yc(1), operation="create_event"),
          _ma("c", "email-marketing-strategist", "Confirmation", _yc(2), "Write a friendly event confirmation with the details."),
          _mn("d", "gmail", "Send confirm", _yc(3), operation="send")]),
    {"id": "m-pr-distribute", "name": "PR announcement distribution",
     "description": "Draft the announcement, approve it, then blast Slack + email.",
     "graph": {"nodes": [
         _mn("a", "manualTrigger", "Start", _yc(0)),
         _ma("b", "pr-communications-manager", "Draft release", _yc(1), "Draft the press release and the key messaging angle."),
         _mn("c", "approval", "Approve release?", _yc(2)),
         _mn("d", "slack", "Notify team", _yc(3), x=320, message="Announcement live: {{upstream}}"),
         _mn("e", "gmail", "Email press list", _yc(3), x=60, operation="send")],
      "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"},
                {"from": "c", "to": "d", "branch": "approved"}, {"from": "c", "to": "e", "branch": "approved"}]}},
    _lin("m-competitor-watch", "Competitor monitoring digest",
         "Scrape competitor pages, summarize the changes, and email a digest.",
         [_mn("a", "manualTrigger", "Every morning", _yc(0)),
          _mn("b", "http", "Fetch competitor", _yc(1), method="GET", url="https://competitor.example.com"),
          _mn("c", "html", "To text", _yc(2), mode="text"),
          _mn("d", "transform", "Summarize changes", _yc(3), instruction="Summarize what changed vs a typical page in 3 bullets."),
          _mn("e", "email", "Email digest", _yc(4))]),
    _lin("m-lead-magnet", "Lead magnet delivery",
         "Deliver the gated asset, then tag the contact in the CRM.",
         [_mn("a", "webhookTrigger", "Asset requested", _yc(0)),
          _mn("b", "gdrive", "Get asset link", _yc(1), operation="list_files"),
          _ma("c", "email-marketing-strategist", "Delivery email", _yc(2), "Write the email that delivers the download link."),
          _mn("d", "gmail", "Send asset", _yc(3), operation="send"),
          _mn("e", "hubspot", "Tag contact", _yc(4), operation="create_contact")]),
    _lin("m-monthly-report", "Monthly marketing report",
         "Pull the numbers, write the narrative, and share via email + Slack.",
         [_mn("a", "manualTrigger", "1st of month", _yc(0)),
          _mn("b", "analysis", "Pull KPIs", _yc(1)),
          _ma("c", "growth-hacker", "Write narrative", _yc(2), "Turn these KPIs into an exec-ready monthly marketing narrative."),
          _mn("d", "email", "Email report", _yc(3)),
          _mn("e", "slack", "Post summary", _yc(4), message="{{upstream}}")]),
    _lin("m-shortvideo-plan", "Short-video content plan",
         "Plan a TikTok/Reels batch and store the shot list in Notion.",
         [_mn("a", "manualTrigger", "Start", _yc(0)),
          _ma("b", "tiktok-strategist", "Concepts", _yc(1), "Propose 8 short-video concepts with hooks for this month."),
          _ma("c", "short-video-editing-coach", "Shot lists", _yc(2), "Turn each concept into a shot list and edit direction."),
          _mn("d", "notion", "Save plan", _yc(3), operation="create_page")]),
    {"id": "m-reengage-ab", "name": "Re-engagement A/B with approval",
     "description": "Write two variants for a dormant list, approve the winner, then send.",
     "graph": {"nodes": [
         _mn("a", "manualTrigger", "Start", _yc(0)),
         _mn("b", "gsheets", "Read dormant list", _yc(1), operation="append"),
         _ma("c", "email-marketing-strategist", "Write A + B", _yc(2), "Write two subject/body variants (A and B) for a re-engagement email."),
         _mn("d", "approval", "Pick + approve", _yc(3)),
         _mn("e", "gmail", "Send winner", _yc(4), operation="send")],
      "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}, {"from": "c", "to": "d"},
                {"from": "d", "to": "e", "branch": "approved"}]}},
    _lin("m-social-listen", "Social listening → escalate",
         "Monitor brand mentions, judge sentiment, and escalate the negative ones.",
         [_mn("a", "manualTrigger", "Every hour", _yc(0)),
          _mn("b", "http", "Fetch mentions", _yc(1), method="GET", url="https://api.example.com/mentions"),
          _ma("c", "x-twitter-intelligence-analyst", "Score sentiment", _yc(2), "Classify each mention's sentiment and flag anything negative or risky."),
          _mn("d", "slack", "Escalate", _yc(3), message="Negative mentions to review: {{upstream}}")]),
]

# Loop templates need the branch edge into the loop body + a done edge.
for _t in _MKT_TEMPLATES:
    _ns = _t["graph"]["nodes"]
    _types = {n["id"]: n["type"] for n in _ns}
    _fixed = []
    for e in _t["graph"]["edges"]:
        if _types.get(e["from"]) == "loop" and "branch" not in e:
            e = dict(e, branch="loop")
        _fixed.append(e)
    _t["graph"]["edges"] = _fixed

_GRAPH_TEMPLATES = _MKT_TEMPLATES + _GRAPH_TEMPLATES


# ── Templates modeled on n8n's Marketing + Social-Media categories ──────────
_MKT_SOCIAL = [
    _lin("n8-linkedin-gpt", "LinkedIn content creation with AI + image",
         "Generate trending LinkedIn topics, write the post in your voice, add an image, and publish on schedule.",
         [_mn("a", "manualTrigger", "Daily schedule", _yc(0)),
          _ma("b", "linkedin-content-creator", "Trend + hook", _yc(1), "Find a trending topic and hook for a LinkedIn post today."),
          _ma("c", "content-creator", "Write post + image brief", _yc(2), "Write the LinkedIn post in an authentic voice and describe an image."),
          _mn("d", "http", "Post to LinkedIn", _yc(3), method="POST", url="https://api.linkedin.com/v2/ugcPosts")]),
    {"id": "n8-multiplatform-ai", "name": "Multi-platform social posts from one idea",
     "description": "Turn one idea into platform-tailored posts for X, LinkedIn, Instagram and Facebook.",
     "graph": {"nodes": [
         _mn("a", "manualTrigger", "Start", _yc(0)),
         _ma("b", "social-media-strategist", "Adapt per platform", _yc(1), "Rewrite this idea into 4 platform-specific posts: X, LinkedIn, Instagram, Facebook."),
         _mn("c", "switch", "Platform", _yc(2), value="{{$json.platform}}", cases="x,linkedin,instagram,facebook"),
         _mn("d", "http", "Post to X", _yc(3), x=380, method="POST", url="https://api.twitter.com/2/tweets"),
         _mn("e", "http", "Post to LinkedIn", _yc(3), x=140, method="POST", url="https://api.linkedin.com/v2/ugcPosts"),
         _mn("f", "http", "Post to Instagram", _yc(3), x=-100, method="POST", url="https://graph.facebook.com/v19.0/me/media"),
         _mn("g", "http", "Post to Facebook", _yc(3), x=-340, method="POST", url="https://graph.facebook.com/v19.0/me/feed")],
      "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"},
                {"from": "c", "to": "d", "branch": "x"}, {"from": "c", "to": "e", "branch": "linkedin"},
                {"from": "c", "to": "f", "branch": "instagram"}, {"from": "c", "to": "g", "branch": "facebook"}]}},
    _lin("n8-trends-post", "Google Trends → AI post automation",
         "Discover a trending query, research it, and auto-publish a formatted social post.",
         [_mn("a", "manualTrigger", "Hourly", _yc(0)),
          _mn("b", "http", "Fetch Google Trends", _yc(1), method="GET", url="https://trends.google.com/trends/api/dailytrends"),
          _ma("c", "content-creator", "Research + write", _yc(2), "Turn the top trend into a professionally formatted social post."),
          _mn("d", "http", "Publish post", _yc(3), method="POST", url="https://graph.facebook.com/v19.0/me/feed")]),
    _lin("n8-newsletter-gpt", "Newsletter creation & client delivery",
         "Draft the newsletter with AI, archive it to Drive, and deliver via Gmail.",
         [_mn("a", "manualTrigger", "Weekly", _yc(0)),
          _mn("b", "gsheets", "Read subscribers", _yc(1), operation="append"),
          _ma("c", "email-marketing-strategist", "Write newsletter", _yc(2), "Write this week's newsletter with a subject line and 3 sections."),
          _mn("d", "gdrive", "Archive to Drive", _yc(3), operation="list_files"),
          _mn("e", "gmail", "Deliver", _yc(4), operation="send")]),
    _lin("n8-drive-shorts", "Drive → Instagram/TikTok/YouTube with AI captions",
         "Detect a new video in Drive, generate an AI description, publish to short-video platforms, and log to Airtable.",
         [_mn("a", "manualTrigger", "New video in Drive", _yc(0)),
          _mn("b", "gdrive", "Get video", _yc(1), operation="list_files"),
          _ma("c", "short-video-editing-coach", "AI description", _yc(2), "Write a hook + description + hashtags for this short video."),
          _mn("d", "http", "Publish to platforms", _yc(3), method="POST", url="https://graph.facebook.com/v19.0/me/media"),
          _mn("e", "airtable", "Track post", _yc(4), operation="append")]),
    _lin("n8-affiliate", "Affiliate product → AI post automation",
         "New affiliate link becomes an AI-captioned social post with an image.",
         [_mn("a", "manualTrigger", "New product link", _yc(0)),
          _mn("b", "gsheets", "Read links", _yc(1), operation="append"),
          _ma("c", "content-creator", "Caption + image", _yc(2), "Write an engaging caption and image idea for this product link."),
          _mn("d", "http", "Post to Facebook", _yc(3), method="POST", url="https://graph.facebook.com/v19.0/me/feed")]),
    {"id": "n8-email-followup", "name": "Email campaign analysis → smart follow-ups",
     "description": "Track campaign engagement and auto-trigger follow-ups for low-engagement contacts.",
     "graph": {"nodes": [
         _mn("a", "manualTrigger", "Daily", _yc(0)),
         _mn("b", "http", "Fetch campaign metrics", _yc(1), method="GET", url="https://api.example.com/email/metrics"),
         _ma("c", "email-marketing-strategist", "Analyze engagement", _yc(2), "Identify low-engagement contacts and draft a follow-up."),
         _mn("d", "if", "Low engagement?", _yc(3), left="{{$json.open_rate}}", op="lt", right="0.2"),
         _mn("e", "gmail", "Send follow-up", _yc(4), x=320, operation="send"),
         _mn("f", "noop", "No action", _yc(4), x=60)],
      "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}, {"from": "c", "to": "d"},
                {"from": "d", "to": "e", "branch": "true"}, {"from": "d", "to": "f", "branch": "false"}]}},
    _lin("n8-weekly-brief", "Weekly industry briefing via AI search",
         "Search the week's industry news, summarize with AI, and email a styled briefing.",
         [_mn("a", "manualTrigger", "Every Monday", _yc(0)),
          _mn("b", "http", "Search news", _yc(1), method="GET", url="https://api.tavily.com/search"),
          _ma("c", "growth-hacker", "Summarize", _yc(2), "Summarize into an emoji-styled weekly briefing with must-reads."),
          _mn("d", "gmail", "Email briefing", _yc(3), operation="send")]),
    _lin("n8-blog-thread", "Blog post → X/Twitter thread",
         "Fetch a new blog post and turn it into an engaging Twitter/X thread.",
         [_mn("a", "manualTrigger", "New post", _yc(0)),
          _mn("b", "http", "Fetch post", _yc(1), method="GET", url="https://blog.example.com/latest"),
          _mn("c", "html", "To text", _yc(2), mode="text"),
          _ma("d", "twitter-engager", "Write thread", _yc(3), "Turn this into a 6-tweet thread with a strong hook."),
          _mn("e", "http", "Post thread", _yc(4), method="POST", url="https://api.twitter.com/2/tweets")]),
    _lin("n8-reddit-listen", "Reddit monitoring → sentiment → Slack",
         "Monitor subreddits for brand mentions, judge sentiment, and alert the team.",
         [_mn("a", "manualTrigger", "Hourly", _yc(0)),
          _mn("b", "http", "Fetch mentions", _yc(1), method="GET", url="https://oauth.reddit.com/search"),
          _ma("c", "reddit-community-builder", "Sentiment + reply ideas", _yc(2), "Classify sentiment and suggest a community-appropriate response."),
          _mn("d", "slack", "Alert team", _yc(3), message="{{upstream}}")]),
    _lin("n8-tiktok-plan", "TikTok trend tracker → content ideas",
         "Track trending sounds/hashtags and turn them into a shootable content plan.",
         [_mn("a", "manualTrigger", "Daily", _yc(0)),
          _mn("b", "http", "Fetch TikTok trends", _yc(1), method="GET", url="https://open.tiktokapis.com/v2/research/"),
          _ma("c", "tiktok-strategist", "Content ideas", _yc(2), "Turn these trends into 6 TikTok concepts with hooks."),
          _mn("d", "notion", "Save plan", _yc(3), operation="create_page")]),
    _lin("n8-calendar-post", "Scheduled multi-channel posting from a calendar",
         "Read today's rows from a content calendar and post each to its channel.",
         [_mn("a", "manualTrigger", "Every morning", _yc(0)),
          _mn("b", "gsheets", "Read calendar", _yc(1), operation="append"),
          _mn("c", "filter", "Due today", _yc(2), left="{{$json.date}}", op="eq", right="today"),
          _mn("d", "http", "Publish", _yc(3), method="POST", url="https://graph.facebook.com/v19.0/me/feed")]),
    _lin("n8-yt-repurpose", "YouTube video → social snippets",
         "Take a new YouTube upload, summarize it, and cut social snippets for each channel.",
         [_mn("a", "manualTrigger", "New upload", _yc(0)),
          _mn("b", "http", "Fetch video meta", _yc(1), method="GET", url="https://www.googleapis.com/youtube/v3/videos"),
          _ma("c", "video-optimization-specialist", "Snippets + titles", _yc(2), "Write 5 short social snippets and title options from this video."),
          _mn("d", "notion", "Save snippets", _yc(3), operation="create_page")]),
    _lin("n8-carousel", "Carousel post generator",
         "Turn a topic into a multi-slide carousel with copy per slide and save the plan.",
         [_mn("a", "manualTrigger", "Start", _yc(0)),
          _ma("b", "carousel-growth-engine", "Design carousel", _yc(1), "Design an 8-slide carousel with copy for each slide on this topic."),
          _mn("c", "gdrive", "Save assets", _yc(2), operation="list_files"),
          _mn("d", "http", "Post to Instagram", _yc(3), method="POST", url="https://graph.facebook.com/v19.0/me/media")]),
    {"id": "n8-ig-dm", "name": "Instagram DM auto-responder",
     "description": "Reply to inbound Instagram DMs with an AI answer, escalating complex ones to a human.",
     "graph": {"nodes": [
         _mn("a", "webhookTrigger", "New DM", _yc(0)),
         _ma("b", "instagram-curator", "Draft reply", _yc(1), "Draft a helpful on-brand reply to this DM."),
         _mn("c", "if", "Needs a human?", _yc(2), left="{{$json.confidence}}", op="lt", right="0.6"),
         _mn("d", "slack", "Escalate", _yc(3), x=320, message="DM needs a human: {{upstream}}"),
         _mn("e", "http", "Send reply", _yc(3), x=60, method="POST", url="https://graph.facebook.com/v19.0/me/messages")],
      "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"},
                {"from": "c", "to": "d", "branch": "true"}, {"from": "c", "to": "e", "branch": "false"}]}},
    _lin("n8-seo-repurpose", "Keyword → blog outline → social teasers",
         "Pick a keyword, produce an SEO blog outline, and generate social teasers to promote it.",
         [_mn("a", "manualTrigger", "Start", _yc(0)),
          _ma("b", "seo-specialist", "Blog outline", _yc(1), "Create an SEO-optimized blog outline for this keyword."),
          _ma("c", "social-media-strategist", "Social teasers", _yc(2), "Write 4 social teasers that drive clicks to this article."),
          _mn("d", "notion", "Save package", _yc(3), operation="create_page")]),
]

# Same loop-branch normalization as the marketing set.
for _t in _MKT_SOCIAL:
    _ns = _t["graph"]["nodes"]
    _types = {n["id"]: n["type"] for n in _ns}
    _t["graph"]["edges"] = [dict(e, branch="loop") if (_types.get(e["from"]) == "loop" and "branch" not in e) else e
                            for e in _t["graph"]["edges"]]

_GRAPH_TEMPLATES = _MKT_SOCIAL + _GRAPH_TEMPLATES


class Step(BaseModel):
    agent_slug: str
    task: str
    id: Optional[str] = None


class WorkflowRequest(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = ""
    steps: list                       # [{agent_slug, task, review}] (linear form)
    schedule: Optional[str] = None
    context: Optional[str] = ""        # optional shared goal/brief prepended to step 1
    recipients: Optional[str] = ""     # email the result here on scheduled runs
    nodes: Optional[list] = None       # visual graph nodes (canvas)
    edges: Optional[list] = None       # visual graph edges (canvas)
    notes: Optional[list] = None       # sticky notes (canvas annotations)
    alert_webhook: Optional[str] = None  # POST here if a scheduled run fails
    error_workflow_id: Optional[str] = None  # run this workflow when a run fails (n8n errorWorkflow)


@router.get("")
async def list_workflows():
    data = _load(_WF_PATH, "workflows")
    items = [{"id": w["id"], "name": w["name"], "description": w.get("description", ""),
              "steps": len(w.get("steps", [])), "schedule": w.get("schedule"),
              "last_run": w.get("last_run")} for w in data.get("workflows", [])]
    return {"ok": True, "workflows": items, "count": len(items)}


@router.get("/templates")
async def templates():
    return {"ok": True, "templates": _TEMPLATES, "graph_templates": _GRAPH_TEMPLATES}


@router.post("")
async def save_workflow(body: WorkflowRequest):
    if not (body.name or "").strip():
        return {"error": "Name is required."}
    if not body.steps and not body.nodes:
        return {"error": "Add at least one step or node."}
    data = _load(_WF_PATH, "workflows")
    wid = body.id or uuid.uuid4().hex[:12]
    steps = [{"id": s.get("id") or uuid.uuid4().hex[:8],
              "agent_slug": s["agent_slug"], "task": s.get("task", ""),
              "review": bool(s.get("review"))} for s in body.steps]
    rec = {"id": wid, "name": body.name.strip(), "description": body.description or "",
           "steps": steps, "schedule": body.schedule, "context": body.context or "",
           "recipients": body.recipients or "",
           "nodes": body.nodes or [], "edges": body.edges or [],
           "notes": body.notes or [], "alert_webhook": (body.alert_webhook or "").strip(),
           "error_workflow_id": (body.error_workflow_id or "").strip(),
           "updated_at": int(time.time())}
    wfs = data.setdefault("workflows", [])
    existing = next((i for i, w in enumerate(wfs) if w["id"] == wid), None)
    if existing is not None:
        _snapshot_version(wfs[existing])   # keep the prior state for rollback
        rec["last_run"] = wfs[existing].get("last_run")
        rec["webhook_token"] = wfs[existing].get("webhook_token")
        rec["active"] = wfs[existing].get("active", False)
        rec["production"] = wfs[existing].get("production")      # environment snapshot (preserved)
        rec["project_id"] = wfs[existing].get("project_id", "")  # project assignment (preserved)
        if body.alert_webhook is None:
            rec["alert_webhook"] = wfs[existing].get("alert_webhook", "")
        wfs[existing] = rec
    else:
        wfs.insert(0, rec)
    _save(_WF_PATH, data)
    return {"ok": True, "id": wid}


@router.delete("/{wid}")
async def delete_workflow(wid: str):
    data = _load(_WF_PATH, "workflows")
    before = len(data.get("workflows", []))
    data["workflows"] = [x for x in data.get("workflows", []) if x["id"] != wid]
    _save(_WF_PATH, data)
    return {"ok": True, "deleted": before - len(data["workflows"])}


# ── Execution ────────────────────────────────────────────────────────────────

def _step_message(agent, task, transcript, shared_context=""):
    ctx = ""
    if shared_context:
        ctx += f"Shared brief for the whole team:\n{shared_context}\n\n"
    if transcript and transcript.strip():
        ctx += f"Prior teammates already did the following work — build on it, don't repeat it:\n\n{transcript}\n"
    return (f"{ctx}Your task as the {agent['name']}: {task}\n\n"
            "Be concrete and hand off clear outputs the next teammate can use.")


async def _run_one(slug, task, transcript="", shared_context="", history=None):
    """Run a single agent step given the accumulated transcript. Returns a step dict.
    `history` (optional) seeds prior conversation memory into the agent turn."""
    import agents_api
    agent = agents_api.agent_by_slug(slug)
    if not agent:
        return {"agent_slug": slug, "agent_name": slug, "task": task, "error": "Agent not found."}
    msg = _step_message(agent, task, transcript, shared_context)
    turn = await agents_api.run_agent_turn(agent, msg, history=history, use_tools=True)
    answer = turn.get("answer") or turn.get("error") or ""
    return {"agent_slug": slug, "agent_name": agent["name"], "task": task,
            "answer": answer, "tools_used": turn.get("tools_used", []),
            "trace": turn.get("trace", []), "error": turn.get("error")}


async def _run_steps(steps, shared_context="", name="workflow"):
    """Execute steps in order, threading each agent's output into the next as context."""
    results = []
    transcript = ""
    for st in steps:
        step = await _run_one(st.get("agent_slug"), st.get("task", ""), transcript, shared_context)
        results.append(step)
        transcript += f"— {step['agent_name']} —\n{step.get('answer','')}\n\n"
    return {"ok": True, "name": name, "steps": results,
            "ran_at": int(time.time()), "step_count": len(results)}


# ── Graph execution (visual canvas: nodes + edges) ──────────────────────────

def _eval_cond(left, op, right, upstream):
    def resolve(v):
        s = str(v if v is not None else "")
        return upstream if s.strip() in ("{{upstream}}", "{{input}}", "{{json}}") else s
    l, r = resolve(left), resolve(right)
    op = (op or "contains").lower()
    if op == "contains":     return r.lower() in l.lower()
    if op == "not_contains": return r.lower() not in l.lower()
    if op == "equals":       return l.strip() == r.strip()
    if op == "not_equals":   return l.strip() != r.strip()
    if op == "is_empty":     return not l.strip()
    if op == "is_not_empty": return bool(l.strip())
    try:
        lf, rf = float(l), float(r)
        if op == "gt":  return lf > rf
        if op == "lt":  return lf < rf
        if op == "gte": return lf >= rf
        if op == "lte": return lf <= rf
    except Exception:
        pass
    return False


async def _http_request(node):
    import urllib.request, json as _j, asyncio as _a
    p = node.get("params") or {}
    url = (node.get("url") or p.get("url") or "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "A valid http(s) URL is required."}
    method = (node.get("method") or p.get("method") or "GET").upper()
    headers = p.get("headers") or node.get("headers") or {}
    if isinstance(headers, str):
        try: headers = _j.loads(headers) if headers.strip() else {}
        except Exception: headers = {}
    body = p.get("body") or node.get("body")
    data = body.encode() if isinstance(body, str) and body else None
    if data and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"
    def _do():
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", "replace")[:6000]
    try:
        out = await _a.get_running_loop().run_in_executor(None, _do)
        return {"output": out}
    except Exception as e:
        return {"error": str(e)}


async def _post_json(url, payload, headers=None, method="POST"):
    import urllib.request, json as _j, asyncio as _a
    data = _j.dumps(payload).encode() if payload is not None else None
    hdr = {"Content-Type": "application/json"}
    hdr.update(headers or {})
    def _do():
        req = urllib.request.Request(url, data=data, method=method, headers=hdr)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:4000]
    return await _a.get_running_loop().run_in_executor(None, _do)


async def _exec_chat_webhook(kind, node, upstream):
    """Slack / Teams / Discord via an incoming-webhook credential."""
    cred = _get_cred((node.get("params") or {}).get("credential_id"))
    if not cred:
        return {"error": "Pick a saved webhook credential for this node."}
    url = (cred.get("data") or {}).get("url", "")
    if not url:
        return {"error": "The selected credential has no webhook URL."}
    msg = str((node.get("params") or {}).get("message", "{{upstream}}")).replace("{{upstream}}", upstream).strip()
    if not msg:
        msg = upstream.strip() or "(no content)"
    payload = {"content": msg} if kind == "discord" else {"text": msg}
    try:
        status, resp = await _post_json(url, payload)
        return {"output": f"Posted to {kind} (HTTP {status})."}
    except Exception as e:
        return {"error": str(e)}


async def _exec_airtable(node, upstream):
    p = node.get("params") or {}
    cred = _get_cred(p.get("credential_id"))
    if not cred:
        return {"error": "Pick a saved Airtable token credential."}
    token = (cred.get("data") or {}).get("token", "")
    base, table = p.get("base_id", "").strip(), p.get("table", "").strip()
    if not (token and base and table):
        return {"error": "Airtable needs a token credential, base ID, and table name."}
    import json as _j, urllib.parse as _up
    hdr = {"Authorization": "Bearer " + token}
    url = f"https://api.airtable.com/v0/{base}/{_up.quote(table)}"
    action = p.get("action", "create")
    try:
        if action == "list":
            status, resp = await _post_json(url + "?maxRecords=10", None, hdr, method="GET")
            return {"output": resp}
        fields = p.get("fields") or {}
        if isinstance(fields, str):
            try: fields = _j.loads(fields) if fields.strip() else {}
            except Exception: return {"error": "Fields must be valid JSON, e.g. {\"Name\":\"…\"}."}
        # allow {{upstream}} inside string field values
        fields = {k: (v.replace("{{upstream}}", upstream) if isinstance(v, str) else v) for k, v in fields.items()}
        status, resp = await _post_json(url, {"fields": fields}, hdr)
        return {"output": f"Created Airtable record (HTTP {status})."}
    except Exception as e:
        return {"error": str(e)}


# ── Native app connectors (bearer-token REST wrappers) ───────────────────────
# Each app = base URL + auth + a few operations. For Google apps the credential
# is an OAuth access token (MCP is the easier path for auto-refresh — noted in UI).
_APP_CATALOG = {
    "gmail": {"label": "Gmail", "cred": "google_oauth", "base": "https://gmail.googleapis.com/gmail/v1",
        "ops": {"send": {"label": "Send email", "method": "POST", "path": "/users/me/messages/send", "write": True,
                         "fields": [], "body_tpl": '{"raw":"<base64url RFC822 message>"}'}}},
    "gsheets": {"label": "Google Sheets", "cred": "google_oauth", "base": "https://sheets.googleapis.com/v4",
        "ops": {"append": {"label": "Append row", "method": "POST", "path": "/spreadsheets/{spreadsheet_id}/values/{range}:append",
                           "query": {"valueInputOption": "RAW"}, "fields": ["spreadsheet_id", "range"], "write": True,
                           "body_tpl": '{"values":[["{{$json.a}}","{{$json.b}}"]]}'}}},
    "gcalendar": {"label": "Google Calendar", "cred": "google_oauth", "base": "https://www.googleapis.com/calendar/v3",
        "ops": {"create_event": {"label": "Create event", "method": "POST", "path": "/calendars/{calendar_id}/events",
                                 "fields": ["calendar_id"], "write": True,
                                 "body_tpl": '{"summary":"{{$json.title}}","start":{"dateTime":"2026-01-01T10:00:00Z"},"end":{"dateTime":"2026-01-01T11:00:00Z"}}'}}},
    "gdrive": {"label": "Google Drive", "cred": "google_oauth", "base": "https://www.googleapis.com/drive/v3",
        "ops": {"list_files": {"label": "List files", "method": "GET", "path": "/files", "query": {"pageSize": "20"}, "fields": []}}},
    "notion": {"label": "Notion", "cred": "notion_token", "base": "https://api.notion.com/v1",
        "headers": {"Notion-Version": "2022-06-28"},
        "ops": {"create_page": {"label": "Create page", "method": "POST", "path": "/pages", "write": True, "fields": [],
                                "body_tpl": '{"parent":{"database_id":"<db id>"},"properties":{}}'},
                "query_db": {"label": "Query database", "method": "POST", "path": "/databases/{database_id}/query",
                             "fields": ["database_id"], "write": True, "body_tpl": "{}"}}},
    "hubspot": {"label": "HubSpot", "cred": "hubspot_token", "base": "https://api.hubapi.com",
        "ops": {"create_contact": {"label": "Create contact", "method": "POST", "path": "/crm/v3/objects/contacts", "write": True,
                                   "fields": [], "body_tpl": '{"properties":{"email":"{{$json.email}}"}}'}}},
    "stripe": {"label": "Stripe", "cred": "stripe_key", "base": "https://api.stripe.com/v1",
        "ops": {"list_charges": {"label": "List charges", "method": "GET", "path": "/charges", "query": {"limit": "10"}, "fields": []}}},
    "github": {"label": "GitHub", "cred": "github_token", "base": "https://api.github.com",
        "ops": {"create_issue": {"label": "Create issue", "method": "POST", "path": "/repos/{owner}/{repo}/issues",
                                 "fields": ["owner", "repo"], "write": True,
                                 "body_tpl": '{"title":"{{$json.title}}","body":"{{$json.body}}"}'}}},
}


def _app_build_request(app_id, node, token):
    import urllib.parse as _up
    app = _APP_CATALOG[app_id]
    p = node.get("params") or {}
    op = app["ops"].get(p.get("operation") or next(iter(app["ops"])))
    if not op:
        raise ValueError("Unknown operation.")
    path = op["path"]
    for f in op.get("fields", []):
        path = path.replace("{" + f + "}", _up.quote(str(p.get(f, "")), safe=""))
    url = app["base"] + path
    q = dict(op.get("query", {}))
    if q:
        url += ("&" if "?" in url else "?") + _up.urlencode(q)
    headers = {"Authorization": "Bearer " + token}
    headers.update(app.get("headers", {}))
    body = None
    if op.get("write"):
        body = p.get("body") or op.get("body_tpl", "")
        headers["Content-Type"] = "application/json"
    return op["method"], url, headers, body, app["label"], op["label"]


async def _exec_app(app_id, node):
    p = node.get("params") or {}
    cred = _get_cred(p.get("credential_id"))
    if not cred:
        return {"error": f"Pick a saved {_APP_CATALOG[app_id]['label']} credential."}
    token = (cred.get("data") or {}).get("token", "")
    if not token:
        return {"error": "The selected credential has no token."}
    try:
        method, url, headers, body, applabel, oplabel = _app_build_request(app_id, node, token)
    except Exception as e:
        return {"error": str(e)}
    import urllib.request, asyncio as _a
    data = body.encode() if isinstance(body, str) and body else None

    def _do():
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:5000]
    try:
        status, resp = await _a.get_running_loop().run_in_executor(None, _do)
        return {"output": f"{applabel}: {oplabel} (HTTP {status})", "raw": resp}
    except Exception as e:
        return {"error": f"{applabel} error: {e}"}


async def _exec_airbyte(node):
    """Trigger a sync of an Airbyte connection (any of the 600+ source connectors).
    This brings Airbyte's ingestion catalog into Studio as a workflow step."""
    p = node.get("params") or {}
    cid = (p.get("connection_id") or "").strip()
    if not cid:
        return {"error": "Pick an Airbyte connection to sync (configure them under Connect)."}
    try:
        import airbyte_api
    except Exception:
        return {"error": "Airbyte module unavailable on the server."}
    import asyncio as _a

    def _do():
        return airbyte_api._http("POST", "/jobs", body={"connectionId": cid, "jobType": "sync"})
    try:
        code, data = await _a.get_running_loop().run_in_executor(None, _do)
        if code and code < 300:
            jid = (data or {}).get("jobId") or (data or {}).get("id") if isinstance(data, dict) else None
            return {"output": f"Airbyte sync triggered" + (f" (job {jid})." if jid else "."),
                    "raw": json.dumps(data, default=str)[:500]}
        return {"error": f"Airbyte sync failed (HTTP {code}): {str(data)[:200]}"}
    except Exception as e:
        return {"error": f"Airbyte error: {e}"}


async def _exec_mcp(node, upstream):
    p = node.get("params") or {}
    server = _get_mcp(p.get("server_id"))
    if not server:
        return {"error": "Pick a registered MCP server for this node."}
    tool = (p.get("tool") or "").strip()
    if not tool:
        return {"error": "Pick a tool to call on the MCP server."}
    import json as _j
    args = p.get("arguments") or {}
    if isinstance(args, str):
        raw = args.replace("{{upstream}}", upstream)
        try:
            args = _j.loads(raw) if raw.strip() else {}
        except Exception:
            return {"error": "Arguments must be valid JSON."}
    else:
        args = {k: (v.replace("{{upstream}}", upstream) if isinstance(v, str) else v) for k, v in args.items()}
    try:
        import mcp_client
    except Exception:
        return {"error": "MCP client module unavailable on the server."}
    return await mcp_client.call_tool(server, tool, args)


async def _llm_transform(instruction, upstream, provider_id=None):
    system = "You transform text as instructed. Output only the transformed result."
    messages = [{"role": "user", "content": f"Instruction: {instruction}\n\nInput:\n{upstream}"}]
    # If a specific provider is chosen (Claude/OpenAI/Gemini/local Ollama), route to it.
    if provider_id:
        prov = _get_llm_provider(provider_id)
        if prov:
            import llm_router
            return await llm_router.complete(prov, system, messages, max_tokens=1200)
        return {"error": "Selected LLM provider not found."}
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return {"error": "AI not configured — pick an LLM provider on the node or set ANTHROPIC_API_KEY."}
    try:
        import anthropic
        client = system_llm.anthropic_client(api_key=key)
        model = os.getenv("JARVIS_AGENT_MODEL", "claude-sonnet-4-6")
        resp = await client.messages.create(model=model, max_tokens=1200, system=system, messages=messages)
        return {"output": "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")}
    except Exception as e:
        return {"error": str(e)}


# ── Expression engine + item (JSON) data model ──────────────────────────────
def _expr_walk(obj, path):
    toks = re.findall(r'\.([A-Za-z_]\w*)|\[\s*["\']([^"\']+)["\']\s*\]|\[\s*(\d+)\s*\]', path)
    cur = obj
    for a, b, c in toks:
        key = a or b or c
        try:
            if isinstance(cur, list):
                cur = cur[int(key)]
            elif isinstance(cur, dict):
                cur = cur.get(key)
            else:
                return None
        except Exception:
            return None
    return cur


def _first_item(items):
    return (items[0] if items else {}) or {}


def _to_str(v):
    if v is None or isinstance(v, _Undef):
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


import ast as _ast


class _Undef:
    """JS-undefined-like: falsy, chainable, stringifies to ''."""
    def __getattr__(self, k): return self
    def __getitem__(self, k): return self
    def __bool__(self): return False
    def __eq__(self, o): return o is None or isinstance(o, _Undef)
    def __ne__(self, o): return not self.__eq__(o)
    def __str__(self): return ""
    def __len__(self): return 0


_UNDEF = _Undef()


class _DotDict(dict):
    def __getattr__(self, k):
        if k in self:
            return _expr_wrap(self[k])
        return _UNDEF


def _expr_wrap(v):
    if isinstance(v, dict):
        return _DotDict(v)
    return v


_EXPR_ALLOWED = (_ast.Expression, _ast.BoolOp, _ast.BinOp, _ast.UnaryOp, _ast.Compare,
    _ast.IfExp, _ast.Call, _ast.Attribute, _ast.Subscript, _ast.Name, _ast.Load,
    _ast.Constant, _ast.List, _ast.Tuple, _ast.Dict, _ast.And, _ast.Or, _ast.Not,
    _ast.Add, _ast.Sub, _ast.Mult, _ast.Div, _ast.Mod, _ast.Pow, _ast.FloorDiv,
    _ast.USub, _ast.UAdd, _ast.Eq, _ast.NotEq, _ast.Lt, _ast.LtE, _ast.Gt, _ast.GtE,
    _ast.In, _ast.NotIn, _ast.Slice)
_EXPR_METHODS = {"upper", "lower", "strip", "title", "replace", "split", "join",
    "startswith", "endswith", "capitalize", "zfill", "lstrip", "rstrip", "find",
    "count", "format", "get"}
_EXPR_FUNCS = {"len": len, "str": str, "int": int, "float": float, "round": round,
    "abs": abs, "min": min, "max": max, "sum": sum, "bool": bool, "sorted": sorted,
    "_len": len}


def _expr_validate(tree):
    for n in _ast.walk(tree):
        if not isinstance(n, _EXPR_ALLOWED):
            raise ValueError("disallowed " + type(n).__name__)
        if isinstance(n, _ast.Call):
            f = n.func
            if isinstance(f, _ast.Name):
                if f.id not in _EXPR_FUNCS and f.id != "_node":
                    raise ValueError("func " + f.id)
            elif isinstance(f, _ast.Attribute):
                if f.attr not in _EXPR_METHODS:
                    raise ValueError("method " + f.attr)
            else:
                raise ValueError("bad call")
        if isinstance(n, _ast.Attribute) and n.attr.startswith("__"):
            raise ValueError("dunder")


def _expr_pre(expr):
    expr = re.sub(r'\$node\[\s*["\']([^"\']+)["\']\s*\]\.json', r'_node("\1")', expr)
    expr = (expr.replace("$json", "_J").replace("$now", "_now").replace("$today", "_today")
                .replace("$timestamp", "_ts").replace("$items", "_items").replace("$upstream", "_up"))
    expr = re.sub(r'\bupstream\b', "_up", expr)
    expr = re.sub(r'([\w\)\]\"\'\.]+)\.length\b', r'_len(\1)', expr)
    expr = expr.replace("||", " or ")
    return expr


def _safe_expr_eval(expr, flow):
    import datetime as _dt
    items = flow.get("items") or []
    node_map = flow.get("node_map") or {}
    first = _DotDict(items[0]) if (items and isinstance(items[0], dict)) else (items[0] if items else _DotDict())

    def _node(t):
        its = (node_map.get(t) or {}).get("items") or []
        return _DotDict(its[0]) if (its and isinstance(its[0], dict)) else _DotDict()
    ns = dict(_EXPR_FUNCS)
    ns.update({"_J": first, "_node": _node,
               "_now": _dt.datetime.now().isoformat(timespec="seconds"),
               "_today": _dt.date.today().isoformat(),
               "_ts": int(_dt.datetime.now().timestamp()),
               "_items": [_expr_wrap(i) for i in items], "_up": flow.get("upstream", "")})
    tree = _ast.parse(_expr_pre(expr), mode="eval")
    _expr_validate(tree)
    return eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, ns)


def _apply_expr(template, flow):
    """Resolve {{ … }} expressions: $json, $node["Title"].json, $now/$today/$timestamp, upstream.
    Full expressions (arithmetic, ||-defaults, ternary, .length, string methods) via a
    safe AST evaluator; falls back to path resolution for anything it can't parse."""
    if not isinstance(template, str) or "{{" not in template:
        return template
    items = flow.get("items") or []
    node_map = flow.get("node_map") or {}
    upstream = flow.get("upstream", "")
    import datetime as _dt

    def repl(m):
        e = m.group(1).strip()
        # Try the full safe expression evaluator first (arithmetic, methods, ||, ternary).
        try:
            val = _safe_expr_eval(e, flow)
            if not isinstance(val, _Undef):
                return _to_str(val)
        except Exception:
            pass
        if e in ("upstream", "$upstream", "$input"):
            return upstream
        ms = re.match(r'^\$secrets\.([A-Za-z0-9_\-]+)$', e)
        if ms:
            return _get_secret(ms.group(1))
        if e in ("workflow_id", "$workflow_id"):
            return str(flow.get("workflow_id", "") or "")
        if e == "$now":
            return _dt.datetime.now().isoformat(timespec="seconds")
        if e == "$today":
            return _dt.date.today().isoformat()
        if e == "$timestamp":
            return str(int(time.time()))
        mj = re.match(r'^\$json\b(.*)$', e)
        if mj:
            v = _first_item(items) if not mj.group(1) else _expr_walk(_first_item(items), mj.group(1))
            return _to_str(v)
        mn = re.match(r'^\$node\[\s*["\']([^"\']+)["\']\s*\]\.json\b(.*)$', e)
        if mn:
            nd = node_map.get(mn.group(1)) or {}
            its = nd.get("items") or []
            v = _first_item(its) if not mn.group(2) else _expr_walk(_first_item(its), mn.group(2))
            return _to_str(v)
        return m.group(0)  # leave unknown expressions visible
    return re.sub(r'\{\{(.*?)\}\}', repl, template)


def _resolve_value(v, flow):
    if isinstance(v, str):
        return _apply_expr(v, flow)
    if isinstance(v, list):
        return [_resolve_value(x, flow) for x in v]
    if isinstance(v, dict):
        return {k: _resolve_value(x, flow) for k, x in v.items()}
    return v


def _resolve_node(node, flow):
    """Return a shallow copy of the node with all string fields' expressions resolved."""
    rn = dict(node)
    for k in ("url", "message", "task", "recipients", "body"):
        if isinstance(rn.get(k), str):
            rn[k] = _apply_expr(rn[k], flow)
    if isinstance(rn.get("params"), dict):
        rn["params"] = _resolve_value(rn["params"], flow)
    return rn


def _derive_items(res, in_items):
    """If a handler didn't set items, derive a sensible structured payload."""
    if "items" in res and res["items"] is not None:
        return res["items"]
    out = res.get("output", "")
    if isinstance(out, str) and out[:1] in ("{", "["):
        try:
            parsed = json.loads(out)
            return parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            pass
    if out:
        return [{"text": out}]
    return in_items or []


def _items_to_text(items):
    if not items:
        return ""
    if len(items) == 1 and isinstance(items[0], dict) and set(items[0].keys()) == {"text"}:
        return items[0]["text"]
    return json.dumps(items, indent=2, default=str)


def _coerce(v):
    """Turn a resolved string into a typed value when it clearly is one."""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if s[:1] in ("{", "["):
        try:
            return json.loads(s)
        except Exception:
            return v
    try:
        if re.fullmatch(r'-?\d+', s):
            return int(s)
        if re.fullmatch(r'-?\d*\.\d+', s):
            return float(s)
    except Exception:
        pass
    return v


def _run_code_node(code, in_items):
    """Run a user Python snippet with `items` in scope; expects `result` to be set.
    Restricted builtins — this is the Code node (opt-in, runs on the user's own host,
    equivalent to them writing a local script, mirroring n8n's Code node)."""
    if not (code or "").strip():
        return {"items": in_items}
    import math as _math, datetime as _dt
    safe_builtins = {k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k)
                     for k in ("len", "range", "str", "int", "float", "bool", "list", "dict", "tuple",
                               "set", "sum", "min", "max", "sorted", "abs", "round", "enumerate", "zip",
                               "map", "filter", "any", "all", "reversed", "isinstance")}
    g = {"__builtins__": safe_builtins, "math": _math, "json": json, "re": re, "datetime": _dt}
    loc = {"items": [dict(i) if isinstance(i, dict) else i for i in in_items], "result": None}
    try:
        exec(code, g, loc)
    except Exception as e:
        return {"error": f"Code error: {e}"}
    res = loc.get("result")
    if res is None:
        res = loc.get("items")
    if not isinstance(res, list):
        res = [res if isinstance(res, dict) else {"value": res}]
    res = [r if isinstance(r, dict) else {"value": r} for r in res]
    return {"items": res}


def _agent_memory(node: dict, flow: dict = None):
    """Resolve a node's Postgres chat-memory attachment. Returns a dict with the
    loaded history + a handle for saving, or None if memory isn't attached."""
    p = node.get("params") or {}
    mconf = p.get("memory") or {}
    if not mconf.get("enabled") or mconf.get("type") != "postgres":
        return None
    try:
        import agent_memory
    except Exception as e:
        log.warning(f"agent_memory import failed: {e}")
        return None
    cred = _get_cred(mconf.get("credential_id"))
    dsn = agent_memory.dsn_from_cred(cred.get("data")) if cred else ""
    # session key can use expressions ({{workflow_id}}, {{$json.email}} …)
    sk_tpl = mconf.get("session_key") or "{{workflow_id}}"
    session_key = _apply_expr(sk_tpl, flow or {}) or (node.get("id") or "default")
    if not str(session_key).strip():
        session_key = node.get("id") or "default"
    window = int(mconf.get("window") or 10)
    try:
        history = agent_memory.load(dsn, session_key, window)
        backend = agent_memory.backend_kind(dsn)
    except Exception as e:
        log.warning(f"memory load failed: {e}")
        history, backend = [], "sqlite"
    return {"dsn": dsn, "session_key": session_key, "history": history, "backend": backend}


def _agent_memory_save(mem: dict, user_msg: str, answer: str):
    try:
        import agent_memory
        agent_memory.append(mem["dsn"], mem["session_key"], "user", user_msg)
        if answer:
            agent_memory.append(mem["dsn"], mem["session_key"], "assistant", answer)
    except Exception as e:
        log.warning(f"memory save failed: {e}")


async def _exec_node(node: dict, upstream: str, shared_context: str, flow: dict = None) -> dict:
    """Execute one graph node given the concatenated outputs of its parents.
    Branching nodes also return a 'branch' decision (if/switch) or 'pass' (filter).
    `flow` carries the structured item channel: {items, node_map, upstream}."""
    ntype = node.get("type", "agent")
    title = node.get("title") or node.get("agent_slug") or ntype
    p = node.get("params") or {}
    flow = flow or {"items": [], "node_map": {}, "upstream": upstream}
    in_items = flow.get("items") or []
    try:
        # ── logic / flow nodes ──
        if ntype == "if":
            ok = _eval_cond(p.get("left", "{{upstream}}"), p.get("op", "contains"), p.get("right", ""), upstream)
            return {"type": "if", "title": title, "output": upstream, "items": in_items,
                    "branch": "true" if ok else "false",
                    "detail": ("condition met → true" if ok else "condition not met → false")}
        if ntype == "switch":
            rules = p.get("rules") or []
            chosen = "default"
            for i, rv in enumerate(rules):
                if _eval_cond(upstream, "contains", rv, upstream):
                    chosen = f"case-{i}"; break
            return {"type": "switch", "title": title, "output": upstream, "items": in_items,
                    "branch": chosen, "detail": f"matched {chosen}"}
        if ntype == "filter":
            ok = _eval_cond(p.get("left", "{{upstream}}"), p.get("op", "is_not_empty"), p.get("right", ""), upstream)
            return {"type": "filter", "title": title, "output": (upstream if ok else ""),
                    "items": (in_items if ok else []), "pass": ok,
                    "detail": ("passed" if ok else "filtered out")}
        if ntype == "merge":
            return {"type": "merge", "title": title, "output": upstream, "items": in_items}
        if ntype == "noop":
            return {"type": "noop", "title": title, "output": upstream, "items": in_items}
        if ntype == "stopAndError":
            return {"type": "stopAndError", "title": title, "output": "",
                    "error": p.get("message") or "Stopped by Stop-and-Error node."}
        if ntype == "wait":
            import asyncio as _a
            secs = 0.0
            try:
                secs = float(p.get("seconds", 0) or 0)
            except Exception:
                secs = 0.0
            await _a.sleep(max(0.0, min(secs, 30)))  # capped for safety
            return {"type": "wait", "title": title, "output": upstream, "items": in_items,
                    "detail": f"waited {secs}s"}
        if ntype == "set":
            fields = p.get("fields")
            if isinstance(fields, dict) and fields:
                base = in_items or [{}]
                out_items = []
                for it in base:
                    ni = dict(it) if isinstance(it, dict) else {"value": it}
                    ni.update({k: _coerce(v) for k, v in fields.items()})
                    out_items.append(ni)
                return {"type": "set", "title": title, "items": out_items, "output": _items_to_text(out_items)}
            tmpl = p.get("value", "")
            txt = str(tmpl).replace("{{upstream}}", upstream)
            return {"type": "set", "title": title, "output": txt, "items": [{"text": txt}]}
        if ntype == "sort":
            field = p.get("field", ""); order = (p.get("order") or "asc").lower()
            def _key(it):
                v = it.get(field) if isinstance(it, dict) else it
                try:
                    return (0, float(v))
                except Exception:
                    return (1, _to_str(v))
            out_items = sorted(in_items, key=_key, reverse=(order == "desc"))
            return {"type": "sort", "title": title, "items": out_items, "output": _items_to_text(out_items)}
        if ntype == "limit":
            try:
                cnt = int(p.get("count", 10) or 10)
            except Exception:
                cnt = 10
            keep = (p.get("keep") or "first").lower()
            out_items = in_items[-cnt:] if keep == "last" else in_items[:cnt]
            return {"type": "limit", "title": title, "items": out_items, "output": _items_to_text(out_items)}
        if ntype == "removeDuplicates":
            field = p.get("field", "")
            seen, out_items = set(), []
            for it in in_items:
                key = _to_str(it.get(field)) if (field and isinstance(it, dict)) else _to_str(it)
                if key not in seen:
                    seen.add(key); out_items.append(it)
            return {"type": "removeDuplicates", "title": title, "items": out_items, "output": _items_to_text(out_items)}
        if ntype == "aggregate":
            field = p.get("field", ""); op = (p.get("op") or "list").lower()
            vals = [(it.get(field) if isinstance(it, dict) else it) for it in in_items]
            if op == "count":
                agg = len(in_items)
            elif op in ("sum", "avg"):
                nums = []
                for v in vals:
                    try: nums.append(float(v))
                    except Exception: pass
                agg = (sum(nums) if op == "sum" else (sum(nums) / len(nums) if nums else 0))
            elif op == "concat":
                agg = (p.get("sep") or ", ").join(_to_str(v) for v in vals)
            else:  # list
                agg = vals
            item = {(field or "result"): agg}
            return {"type": "aggregate", "title": title, "items": [item], "output": _items_to_text([item])}
        if ntype == "splitOut":
            field = p.get("field", "")
            out_items = []
            for it in in_items:
                arr = it.get(field) if isinstance(it, dict) else None
                if isinstance(arr, list):
                    for el in arr:
                        out_items.append(el if isinstance(el, dict) else {(field or "value"): el})
                elif arr is not None:
                    out_items.append(arr if isinstance(arr, dict) else {(field or "value"): arr})
            return {"type": "splitOut", "title": title, "items": out_items, "output": _items_to_text(out_items)}
        if ntype == "renameKeys":
            mapping = p.get("map") or {}
            if isinstance(mapping, str):
                try: mapping = json.loads(mapping) if mapping.strip() else {}
                except Exception: mapping = {}
            out_items = []
            for it in in_items:
                if isinstance(it, dict):
                    out_items.append({mapping.get(k, k): v for k, v in it.items()})
                else:
                    out_items.append(it)
            return {"type": "renameKeys", "title": title, "items": out_items, "output": _items_to_text(out_items)}
        if ntype == "dateTime":
            import datetime as _dt
            fmt = p.get("format") or ""
            src = p.get("value")
            now = _dt.datetime.now()
            if src:
                out = _apply_expr(str(src), flow) if isinstance(src, str) else str(src)
            else:
                out = now.strftime(fmt) if fmt else now.isoformat(timespec="seconds")
            fld = p.get("field") or "date"
            item = {fld: out}
            return {"type": "dateTime", "title": title, "items": [item], "output": out}
        if ntype == "html":
            mode = (p.get("mode") or "text").lower()
            html_in = flow.get("raw") or upstream
            if mode == "extract":
                tag = p.get("tag") or "p"
                matches = re.findall(r'<' + re.escape(tag) + r'[^>]*>(.*?)</' + re.escape(tag) + r'>', html_in, re.S | re.I)
                cleaned = [re.sub(r'<[^>]+>', '', mm).strip() for mm in matches]
                out_items = [{"text": c} for c in cleaned if c]
                return {"type": "html", "title": title, "items": out_items, "output": _items_to_text(out_items)}
            txt = re.sub(r'<[^>]+>', ' ', html_in)
            txt = re.sub(r'\s+', ' ', txt).strip()
            return {"type": "html", "title": title, "items": [{"text": txt}], "output": txt}
        if ntype == "extractFile":
            fmt = (p.get("format") or "csv").lower()
            data = flow.get("raw") or upstream
            if fmt == "json":
                try:
                    parsed = json.loads(data)
                    out_items = parsed if isinstance(parsed, list) else [parsed]
                except Exception as e:
                    return {"type": "extractFile", "title": title, "output": "", "error": f"Invalid JSON: {e}"}
            else:  # csv
                import csv, io
                try:
                    rows = list(csv.DictReader(io.StringIO(data)))
                    out_items = [dict(r) for r in rows]
                except Exception as e:
                    return {"type": "extractFile", "title": title, "output": "", "error": f"CSV parse failed: {e}"}
            return {"type": "extractFile", "title": title, "items": out_items,
                    "output": f"Parsed {len(out_items)} rows.", "detail": f"{len(out_items)} items"}
        if ntype == "code":
            r = _run_code_node(p.get("code", ""), in_items)
            if r.get("error"):
                return {"type": "code", "title": title, "output": "", "error": r["error"]}
            return {"type": "code", "title": title, "items": r["items"], "output": _items_to_text(r["items"])}
        if ntype == "http":
            r = await _http_request(node)
            return {"type": "http", "title": title, "output": r.get("output", ""), "error": r.get("error")}
        if ntype in ("slack", "teams", "discord"):
            r = await _exec_chat_webhook(ntype, node, upstream)
            return {"type": ntype, "title": title, "output": r.get("output", ""), "error": r.get("error")}
        if ntype == "airtable":
            r = await _exec_airtable(node, upstream)
            return {"type": "airtable", "title": title, "output": r.get("output", ""), "error": r.get("error")}
        if ntype == "mcp":
            r = await _exec_mcp(node, upstream)
            return {"type": "mcp", "title": title, "output": r.get("output", ""), "error": r.get("error")}
        if ntype in _APP_CATALOG:
            r = await _exec_app(ntype, node)
            return {"type": ntype, "title": title, "output": r.get("output", ""),
                    "detail": (r.get("raw") or "")[:200], "error": r.get("error")}
        if ntype == "airbyte":
            r = await _exec_airbyte(node)
            return {"type": "airbyte", "title": title, "output": r.get("output", ""),
                    "detail": (r.get("raw") or "")[:200], "error": r.get("error")}
        if ntype == "respond":
            val = p.get("value")
            out = _apply_expr(str(val), flow) if val else (upstream or _items_to_text(in_items))
            return {"type": "respond", "title": title, "output": out, "items": in_items, "respond": True}
        if ntype == "approval":
            nid = node.get("id")
            dec = (flow.get("decisions") or {}).get(nid)
            if dec in ("approved", "rejected"):
                return {"type": "approval", "title": title, "output": upstream, "items": in_items,
                        "branch": dec, "detail": f"decision: {dec}"}
            # Undecided → pause the workflow here and wait for a human.
            return {"type": "approval", "title": title, "output": upstream, "items": in_items,
                    "paused": True, "detail": (p.get("message") or "Waiting for approval")}
        if ntype == "subworkflow":
            w = _load_workflow(p.get("workflow_id"))
            if not w:
                return {"type": "subworkflow", "title": title, "output": "", "error": "Pick a workflow to run."}
            depth = flow.get("depth", 0)
            if depth >= 5:
                return {"type": "subworkflow", "title": title, "output": "", "error": "Sub-workflow nesting too deep."}
            sub = await _run_graph(w.get("nodes", []), w.get("edges", []), shared_context,
                                   seed_items=in_items, return_items=True, _depth=depth + 1)
            if sub.get("error"):
                return {"type": "subworkflow", "title": title, "output": "", "error": sub["error"]}
            agg = []
            for tnid in sub.get("terminals", []):
                agg.extend(sub.get("item_map", {}).get(tnid, []))
            resp = next((nn for nn in sub.get("nodes", []) if nn.get("respond")), None)
            out = resp.get("output") if resp else _items_to_text(agg)
            return {"type": "subworkflow", "title": title, "items": agg, "output": out,
                    "detail": f"ran '{w.get('name','workflow')}'"}
        if ntype == "transform":
            r = await _llm_transform(p.get("instruction", "Summarize the input."), upstream, p.get("provider_id"))
            return {"type": "transform", "title": title, "output": r.get("output", ""), "error": r.get("error")}
        if ntype == "agent":
            mem = _agent_memory(node, flow)      # Postgres (or SQLite-fallback) chat memory, if attached
            history = mem["history"] if mem else None
            user_msg = (node.get("task", "") + "\n\n" + upstream).strip()
            pid = p.get("provider_id")
            prov = _get_llm_provider(pid) if pid else None
            if prov:  # run this agent on the chosen model (tool-free, e.g. OpenAI/Gemini/local Ollama)
                import agents_api, llm_router
                ag = agents_api.agent_by_slug(node.get("agent_slug"))
                sysp = (f"You ARE the '{ag['name']}' agent. Role: {ag.get('role','')}\n{ag.get('body','')[:2500]}"
                        if ag else "You are a helpful specialist agent.")
                msgs = (history or []) + [{"role": "user", "content": user_msg}]
                r = await llm_router.complete(prov, sysp, msgs, max_tokens=1400)
                out = r.get("output", "")
                if mem and not r.get("error"):
                    _agent_memory_save(mem, user_msg, out)
                return {"type": "agent", "title": (ag or {}).get("name", title),
                        "output": out, "error": r.get("error"),
                        "detail": (f"model: {prov.get('name')}" + (f" · memory: {mem['backend']}" if mem else ""))}
            step = await _run_one(node.get("agent_slug"), node.get("task", ""), upstream, shared_context,
                                  history=history)
            out = step.get("answer") or step.get("error") or ""
            if mem and not step.get("error"):
                _agent_memory_save(mem, user_msg, step.get("answer") or "")
            return {"type": "agent", "title": step.get("agent_name", title),
                    "output": out, "tools_used": step.get("tools_used", []),
                    "trace": step.get("trace", []), "error": step.get("error"),
                    "detail": (f"memory: {mem['backend']}" if mem else None)}
        if ntype in ("trigger", "note", "manualTrigger", "webhookTrigger", "formTrigger", "errorTrigger", "chatTrigger"):
            # Triggers pass seed items through (webhook/form bodies), else pinned test data.
            items = in_items
            if not items and p.get("pinned"):
                try:
                    parsed = json.loads(p["pinned"])
                    items = parsed if isinstance(parsed, list) else [parsed]
                except Exception:
                    items = [{"text": p["pinned"]}]
            out = node.get("task", "") or (_items_to_text(items) if items else "") or shared_context or ""
            return {"type": ntype, "title": title, "output": out, "items": items}
        if ntype == "email":
            recips = (node.get("recipients") or "").strip()
            if not recips:
                return {"type": "email", "title": title, "output": "", "error": "No recipients set."}
            run = {"name": title, "steps": [{"agent_name": "Upstream", "answer": upstream}]}
            subject, text, html = _run_email_html(run)
            import smtp_mailer, asyncio as _a
            if not smtp_mailer.is_configured():
                return {"type": "email", "title": title, "output": "", "error": "SMTP not configured."}
            sent = await _a.get_running_loop().run_in_executor(None, lambda: smtp_mailer.send(recips, subject, text, html))
            return {"type": "email", "title": title, "output": "Emailed to " + ", ".join(sent)}
        if ntype == "webhook":
            url = (node.get("url") or "").strip()
            if not url:
                return {"type": "webhook", "title": title, "output": "", "error": "No URL set."}
            from alerts import _post_webhook
            import asyncio as _a
            ok = await _a.get_running_loop().run_in_executor(None, _post_webhook, url, upstream[:4000] or title)
            return {"type": "webhook", "title": title, "output": ("Delivered" if ok else "Delivery failed")}
        if ntype == "analysis":
            # direct Test & Learn call with the node's params
            import experiments_api as X
            tool = node.get("tool"); p = node.get("params") or {}
            if tool == "explain_metric":
                r = await X.auto_insights(X.AutoInsightsRequest(**{k: p.get(k) for k in
                    ("primary_table", "metric_field", "date_field", "window_start", "window_end", "dimension_field") if p.get(k) is not None}))
                return {"type": "analysis", "title": title, "output": (r.get("narrative") or r.get("error") or "")}
            if tool == "forecast_metric":
                r = await X.forecast_from_data(X.ForecastFromDataRequest(**{k: p.get(k) for k in
                    ("primary_table", "metric_field", "date_field", "window_start", "window_end", "horizon") if p.get(k) is not None}))
                return {"type": "analysis", "title": title, "output": (r.get("summary") or r.get("error") or "")}
            return {"type": "analysis", "title": title, "output": "", "error": f"Unknown analysis '{tool}'."}
        return {"type": ntype, "title": title, "output": "", "error": f"Unknown node type '{ntype}'."}
    except Exception as e:
        return {"type": ntype, "title": title, "output": "", "error": str(e)}


def _loop_body(loop_id, edges, idmap):
    """Nodes that make up a Loop node's 'loop' sub-branch (reachable from the
    loop output, excluding anything reachable from the 'done' output)."""
    def _br(e):
        return e.get("branch") or e.get("port")
    loop_targets = [e["to"] for e in edges if e.get("from") == loop_id and _br(e) == "loop" and e.get("to") in idmap]
    done_targets = [e["to"] for e in edges if e.get("from") == loop_id and _br(e) in (None, "", "done") and e.get("to") in idmap]

    def _reach(starts):
        seen, stack = set(), list(starts)
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            for e in edges:
                if e.get("from") == x and e.get("to") in idmap:
                    stack.append(e["to"])
        return seen
    done_reach = _reach(done_targets)
    body = _reach(loop_targets) - done_reach - {loop_id}
    return body


async def _run_graph(nodes, edges, shared_context="", seed_items=None, return_items=False, _depth=0, decisions=None):
    """Topologically execute a node graph, threading each node's parents' outputs
    into it. Returns per-node results (in execution order).
    `seed_items` seeds the root nodes' input (used for Loop sub-executions).
    `decisions` injects approval decisions {node_id: 'approved'|'rejected'}."""
    from collections import deque
    idmap = {n["id"]: n for n in nodes if n.get("id")}
    if not idmap:
        return {"error": "No nodes to run."}
    children = {nid: [] for nid in idmap}
    parents = {nid: [] for nid in idmap}
    indeg = {nid: 0 for nid in idmap}
    for e in edges or []:
        a, b = e.get("from"), e.get("to")
        if a in idmap and b in idmap:
            children[a].append(b)
            parents[b].append(a)
            indeg[b] += 1
    q = deque([nid for nid in idmap if indeg[nid] == 0])
    order, indeg2 = [], dict(indeg)
    while q:
        nid = q.popleft(); order.append(nid)
        for c in children[nid]:
            indeg2[c] -= 1
            if indeg2[c] == 0:
                q.append(c)
    if len(order) != len(idmap):
        return {"error": "The graph has a cycle — remove a connection that loops back."}
    # Activation-based execution: a node runs only if it is reachable via edges
    # that were actually "taken". Branching nodes (if/switch/filter) decide which
    # of their out-edges are taken, so downstream branches run conditionally.
    outputs, results, active = {}, [], {}
    node_items = {}          # nid -> structured items[]
    node_map = {}            # title -> {items, output}  (for {{$node["Title"]}})
    taken = set()            # (from, to) edges actually followed
    handled_by_loop = set()  # body nodes executed inside a Loop node
    paused_nodes = []        # approval nodes awaiting a human decision

    def _branch_of(edge):
        return edge.get("branch") or edge.get("port")

    for nid in order:
        n = idmap[nid]
        ps = parents[nid]
        is_active = (len(ps) == 0) or any(active.get(p) and ((p, nid) in taken) for p in ps)
        title = n.get("title") or n.get("agent_slug") or n.get("type") or "node"
        if nid in handled_by_loop:
            is_active = False  # executed inside its Loop; don't run again in the main pass
        if not is_active:
            active[nid] = False
            outputs[nid] = ""
            node_items[nid] = []
            results.append({"id": nid, "type": n.get("type"), "title": title, "skipped": True, "output": ""})
            continue
        active[nid] = True
        upstream = ""
        raw_parts = []
        merged_items = []
        if len(ps) == 0 and seed_items is not None:
            merged_items = list(seed_items)
            raw_parts.append(_items_to_text(seed_items))
            upstream = _items_to_text(seed_items)
        for p in ps:
            if not (active.get(p) and (p, nid) in taken):
                continue
            po = outputs.get(p)
            if po:
                upstream += f"— {idmap[p].get('title') or idmap[p].get('agent_slug') or 'node'} —\n{po}\n\n"
                raw_parts.append(po)
            merged_items.extend(node_items.get(p, []))
        raw = raw_parts[0] if len(raw_parts) == 1 else "\n".join(raw_parts)
        flow = {"items": merged_items, "node_map": node_map, "upstream": upstream, "raw": raw,
                "depth": _depth, "decisions": decisions or {}}

        # ── Loop Over Items: run the 'loop' sub-branch once per input item ──
        if n.get("type") == "loop" and _depth < 5:
            body = _loop_body(nid, edges or [], idmap)
            handled_by_loop |= body
            sub_nodes = [idmap[b] for b in body]
            sub_edges = [e for e in (edges or []) if e.get("from") in body and e.get("to") in body]
            per_item = merged_items or [{}]
            try:
                max_iter = int((n.get("params") or {}).get("max_iterations", 0) or 0)
            except Exception:
                max_iter = 0
            if max_iter > 0:
                per_item = per_item[:max_iter]
            continue_on_fail = (n.get("params") or {}).get("continueOnFail", True)
            agg_items, iters, fails = [], 0, 0
            loop_error = None
            for it in per_item:
                if sub_nodes:
                    sub = await _run_graph(sub_nodes, sub_edges, shared_context, seed_items=[it],
                                           return_items=True, _depth=_depth + 1)
                    it_err = next((nn.get("error") for nn in sub.get("nodes", []) if nn.get("error")), None)
                    if it_err:
                        fails += 1
                        if not continue_on_fail:
                            loop_error = f"Iteration {iters + 1} failed: {it_err}"; iters += 1; break
                    for tnid in sub.get("terminals", []):
                        agg_items.extend(sub.get("item_map", {}).get(tnid, []))
                else:
                    agg_items.append(it)
                iters += 1
            outputs[nid] = _items_to_text(agg_items)
            node_items[nid] = agg_items
            node_map[title] = {"items": agg_items, "output": outputs[nid]}
            detail = f"{iters} iterations" + (f", {fails} failed" if fails else "")
            loop_res = {"id": nid, "type": "loop", "title": title,
                        "output": outputs[nid], "detail": detail,
                        "item_count": len(agg_items), "items_preview": agg_items[:5]}
            if loop_error:
                loop_res["error"] = loop_error
            results.append(loop_res)
            if loop_error:
                # stop-on-fail: don't continue past the loop
                continue
            for e in [e for e in (edges or []) if e.get("from") == nid and e.get("to") in idmap]:
                if _branch_of(e) in (None, "", "done"):
                    taken.add((nid, e["to"]))
            continue
        rnode = _resolve_node(n, flow)
        # Execute with optional retry-on-error (backoff).
        attempts = 0
        try:
            attempts = int((n.get("params") or {}).get("retry", 0) or 0)
        except Exception:
            attempts = 0
        attempts = max(0, min(attempts, 3))
        res = await _exec_node(rnode, upstream, shared_context, flow)
        tries = 1
        while res.get("error") and tries <= attempts:
            import asyncio as _a
            await _a.sleep(min(2 ** tries, 5) * 0.3)
            res = await _exec_node(rnode, upstream, shared_context, flow)
            tries += 1
        if attempts:
            res["attempts"] = tries
        if res.get("error") and (n.get("params") or {}).get("continueOnFail"):
            res["continued"] = True
        outputs[nid] = res.get("output", "")
        node_items[nid] = _derive_items(res, merged_items)
        node_map[title] = {"items": node_items[nid], "output": outputs[nid]}
        _clean = {k: v for k, v in res.items() if k != "items"}
        _clean["item_count"] = len(node_items[nid])
        _clean["items_preview"] = node_items[nid][:5]
        results.append({"id": nid, **_clean})
        # Decide which out-edges are taken.
        out_edges = [e for e in (edges or []) if e.get("from") == nid and e.get("to") in idmap]
        err_edges = [e for e in out_edges if _branch_of(e) == "error"]
        norm_edges = [e for e in out_edges if _branch_of(e) != "error"]
        ntype = n.get("type")
        cof = bool((n.get("params") or {}).get("continueOnFail"))
        if res.get("paused"):
            paused_nodes.append({"node_id": nid, "title": title,
                                 "message": (n.get("params") or {}).get("message") or "Waiting for approval",
                                 "items": node_items[nid]})
            # take no out-edges — the flow stops here until a human decides
        elif res.get("error") and cof:
            res["continued"] = True
            for e in norm_edges + err_edges:
                taken.add((nid, e["to"]))
        elif res.get("error"):
            # On failure, route only down error branches (if any); otherwise the path stops.
            for e in err_edges:
                taken.add((nid, e["to"]))
        elif ntype == "filter":
            if res.get("pass", True):
                for e in norm_edges:
                    taken.add((nid, e["to"]))
        elif res.get("branch") is not None:
            decision = res.get("branch")
            matched = False
            for e in norm_edges:
                if _branch_of(e) == decision:
                    taken.add((nid, e["to"])); matched = True
            if not matched:  # fall through unlabeled/default edges
                for e in norm_edges:
                    if _branch_of(e) in (None, "", "default"):
                        taken.add((nid, e["to"]))
        else:
            for e in norm_edges:
                taken.add((nid, e["to"]))
    ran = sum(1 for r in results if not r.get("skipped"))
    out = {"ok": True, "nodes": results, "order": order, "ran_at": int(time.time()),
           "step_count": len(results), "ran_count": ran}
    if paused_nodes:
        out["paused"] = True
        out["pending"] = paused_nodes
    if return_items:
        has_child = {e.get("from") for e in (edges or []) if e.get("to") in idmap}
        out["terminals"] = [nid for nid in idmap if nid not in has_child]
        out["item_map"] = node_items
    return out


class RunGraphRequest(BaseModel):
    nodes: list
    edges: Optional[list] = None
    context: Optional[str] = ""
    name: Optional[str] = "Graph run"
    id: Optional[str] = None
    decisions: Optional[dict] = None


@router.post("/run_graph")
async def run_graph(body: RunGraphRequest):
    if not body.nodes:
        return {"error": "Add at least one node."}
    res = await _run_graph(body.nodes, body.edges or [], body.context or "", decisions=body.decisions or {})
    if res.get("ok"):
        try:
            _record_graph_run(body.id, body.name or "Graph run", res)
        except Exception as e:
            log.debug("graph run record skipped: %s", e)
        # If the run paused on approval nodes, persist pending approvals + notify.
        if res.get("pending"):
            try:
                snapshot = {"nodes": body.nodes, "edges": body.edges or [], "context": body.context or ""}
                created = _persist_approvals(body.id, body.name or "Graph run", snapshot, res["pending"])
                w = _load_workflow(body.id) if body.id else None
                for rec in created:
                    await _notify_approval(w, rec)
            except Exception as e:
                log.debug("approval persist skipped: %s", e)
    return res


class ChatRunRequest(BaseModel):
    message: str


@router.post("/{wid}/chat")
async def chat_workflow(wid: str, body: ChatRunRequest):
    """Chat trigger: run a saved workflow with a chat message as the seed item, and
    return the workflow's reply (a Respond node's output, else the last output)."""
    w = _load_workflow(wid)
    if not w:
        return {"error": "Not found."}
    msg = (body.message or "").strip()
    if not msg:
        return {"error": "Message required."}
    seed = [{"chatInput": msg, "text": msg, "message": msg}]
    res = await _run_graph(w.get("nodes", []), w.get("edges", []), w.get("context", ""), seed_items=seed)
    nodes = res.get("nodes", [])
    reply = ""
    for n in nodes:
        if n.get("type") == "respond" and n.get("output"):
            reply = n["output"]
    if not reply:
        for n in reversed(nodes):
            if n.get("output"):
                reply = n["output"]; break
    try:
        _record_graph_run(wid, w.get("name", "chat"), res)
    except Exception:
        pass
    return {"ok": res.get("ok", True), "reply": reply, "run": res}


class RunNodeRequest(BaseModel):
    nodes: list
    edges: Optional[list] = None
    context: Optional[str] = ""
    target: str


@router.post("/run_node")
async def run_node(body: RunNodeRequest):
    """Run the graph only up to the target node (its ancestors + itself) and return
    that node's structured output — n8n's 'execute up to here' for quick testing."""
    if not body.target:
        return {"error": "No target node."}
    idmap = {n["id"]: n for n in body.nodes if n.get("id")}
    if body.target not in idmap:
        return {"error": "Target node not found."}
    edges = body.edges or []
    # reverse-reachability: all ancestors that can reach the target
    keep, stack = {body.target}, [body.target]
    while stack:
        cur = stack.pop()
        for e in edges:
            if e.get("to") == cur and e.get("from") in idmap and e["from"] not in keep:
                keep.add(e["from"]); stack.append(e["from"])
    sub_nodes = [idmap[k] for k in keep]
    sub_edges = [e for e in edges if e.get("from") in keep and e.get("to") in keep]
    res = await _run_graph(sub_nodes, sub_edges, body.context or "", return_items=True)
    if res.get("error"):
        return res
    tnode = next((n for n in res.get("nodes", []) if n.get("id") == body.target), None)
    items = res.get("item_map", {}).get(body.target, [])
    return {"ok": True, "target": body.target,
            "output": (tnode or {}).get("output", ""), "error": (tnode or {}).get("error"),
            "item_count": len(items), "items": items[:20]}


def _record_graph_run(wid, name, run):
    data = _load(_RUN_PATH, "runs")
    nodes = run.get("nodes", [])
    rec = {"id": uuid.uuid4().hex[:12], "workflow_id": wid, "name": name,
           "ran_at": run.get("ran_at"), "step_count": run.get("step_count"),
           "ran_count": run.get("ran_count"),
           "failed": any(n.get("error") for n in nodes),
           "summary": " → ".join((n.get("title") or n.get("id") or "?") for n in nodes if not n.get("skipped")),
           "nodes": [{"id": n.get("id"), "title": n.get("title"), "type": n.get("type"),
                      "skipped": n.get("skipped", False), "error": n.get("error"),
                      "branch": n.get("branch"),
                      "output": (n.get("output") or "")[:2000]} for n in nodes]}
    runs = data.setdefault("runs", [])
    runs.insert(0, rec)
    del runs[50:]
    _save(_RUN_PATH, data)


class RunStepRequest(BaseModel):
    agent_slug: str
    task: str
    transcript: Optional[str] = ""      # accumulated outputs from prior steps
    context: Optional[str] = ""         # shared team brief


@router.post("/run_step")
async def run_step(body: RunStepRequest):
    """Run ONE step — lets the UI stream a workflow step-by-step and pause for
    human review between agents."""
    if not body.agent_slug:
        return {"error": "agent_slug required."}
    return await _run_one(body.agent_slug, body.task or "", body.transcript or "", body.context or "")


def _record_run(wid, name, run):
    data = _load(_RUN_PATH, "runs")
    steps = run.get("steps", [])
    failed = bool(run.get("failed") or any(s.get("error") for s in steps))
    rec = {"id": uuid.uuid4().hex[:12], "workflow_id": wid, "name": name,
           "ran_at": run.get("ran_at"), "step_count": run.get("step_count"),
           "failed": failed,
           "summary": " → ".join(s.get("agent_name", "?") for s in steps)}
    runs = data.setdefault("runs", [])
    runs.insert(0, rec)
    del runs[50:]
    _save(_RUN_PATH, data)


async def _notify_failure(w, errored_steps):
    """Alert on a failed scheduled run via email (recipients) and/or an alert webhook."""
    detail = "; ".join(f"{s.get('agent_name','node')}: {s.get('error')}" for s in errored_steps if s.get("error"))[:1000]
    name = w.get("name", "workflow")
    # email
    recips = (w.get("recipients") or "").strip()
    if recips:
        try:
            import smtp_mailer, asyncio as _a
            if smtp_mailer.is_configured():
                subject = f"⚠ Workflow failed: {name}"
                text = f"The scheduled workflow '{name}' failed.\n\n{detail}"
                html = f"<div style='font-family:system-ui'><b>{subject}</b><pre style='white-space:pre-wrap'>{text}</pre></div>"
                await _a.get_running_loop().run_in_executor(None, lambda: smtp_mailer.send(recips, subject, text, html))
        except Exception as e:
            log.debug("failure email skipped: %s", e)
    # webhook
    url = (w.get("alert_webhook") or "").strip()
    if url.startswith("http"):
        try:
            await _post_json(url, {"workflow": name, "status": "failed", "detail": detail})
        except Exception as e:
            log.debug("failure webhook skipped: %s", e)
    # backup / error workflow (n8n's errorWorkflow) — run another workflow on failure,
    # seeded with the error context so it can notify, log, or remediate.
    ew = (w.get("error_workflow_id") or "").strip()
    if ew and ew != w.get("id"):
        try:
            bw = next((x for x in _load(_WF_PATH, "workflows").get("workflows", []) if x["id"] == ew), None)
            if bw and bw.get("nodes"):
                seed = [{"workflow": name, "workflow_id": w.get("id"), "status": "failed", "detail": detail}]
                await _run_graph(bw.get("nodes", []), bw.get("edges", []),
                                 shared_context=f"Error handler for '{name}'. Failure: {detail}",
                                 seed_items=seed)
        except Exception as e:
            log.debug("error workflow skipped: %s", e)


@router.post("/{wid}/run")
async def run_workflow(wid: str):
    data = _load(_WF_PATH, "workflows")
    w = next((x for x in data.get("workflows", []) if x["id"] == wid), None)
    if not w:
        return {"error": "Not found."}
    run = await _run_steps(w.get("steps", []), w.get("context", ""), w.get("name", "workflow"))
    w["last_run"] = int(time.time())
    _save(_WF_PATH, data)
    _record_run(wid, w.get("name"), run)
    return run


class AdhocRunRequest(BaseModel):
    steps: list
    context: Optional[str] = ""
    name: Optional[str] = "Ad-hoc run"


@router.post("/run_adhoc")
async def run_adhoc(body: AdhocRunRequest):
    if not body.steps:
        return {"error": "Add at least one step."}
    return await _run_steps(body.steps, body.context or "", body.name or "Ad-hoc run")


# ── Email a workflow run ─────────────────────────────────────────────────────

def _run_email_html(run: dict) -> tuple:
    name = run.get("name", "Workflow")
    steps = run.get("steps", [])
    def _md_to_html(s):
        s = (s or "")
        s = re.sub(r"&", "&amp;", s); s = re.sub(r"<", "&lt;", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        return "".join(f"<p style='margin:6px 0'>{ln}</p>" for ln in s.split("\n") if ln.strip())
    body = "".join(
        f"<div style='margin:0 0 18px'>"
        f"<div style='font-weight:700;color:#0E76B4;font-size:14px'>{i+1}. {s.get('agent_name','')}</div>"
        f"<div style='color:#334155;font-size:13px;line-height:1.6'>{_md_to_html(s.get('answer') or s.get('error') or '')}</div>"
        f"</div>" for i, s in enumerate(steps))
    html = (f"<div style='font-family:Inter,Arial,sans-serif;max-width:640px'>"
            f"<h2 style='color:#0f172a;margin:0 0 4px'>{name}</h2>"
            f"<div style='color:#64748b;font-size:12px;margin-bottom:16px'>Agent Studio workflow · "
            f"{' → '.join(s.get('agent_name','?') for s in steps)}</div>{body}</div>")
    text = f"{name}\n\n" + "\n\n".join(f"{i+1}. {s.get('agent_name','')}\n{s.get('answer') or s.get('error') or ''}"
                                       for i, s in enumerate(steps))
    return f"Workflow: {name}", text, html


class EmailRunRequest(BaseModel):
    recipients: str
    run: Optional[dict] = None          # a run to email; if absent, runs the saved workflow


@router.post("/{wid}/email")
async def email_workflow(wid: str, body: EmailRunRequest):
    if not (body.recipients or "").strip():
        return {"error": "Recipients required."}
    run = body.run
    if not run:
        data = _load(_WF_PATH, "workflows")
        w = next((x for x in data.get("workflows", []) if x["id"] == wid), None)
        if not w:
            return {"error": "Not found."}
        run = await _run_steps(w.get("steps", []), w.get("context", ""), w.get("name", "workflow"))
    subject, text, html = _run_email_html(run)
    try:
        import smtp_mailer, asyncio as _a
        if not smtp_mailer.is_configured():
            return {"error": "SMTP not configured (set SMTP_HOST / SMTP_FROM)."}
        sent = await _a.get_running_loop().run_in_executor(None, lambda: smtp_mailer.send(body.recipients, subject, text, html))
        return {"ok": True, "detail": "Sent to " + ", ".join(sent)}
    except Exception as e:
        return {"error": str(e)}


# ── AI: suggest a sequence for a goal ────────────────────────────────────────

class SuggestRequest(BaseModel):
    goal: str


@router.post("/suggest")
async def suggest(body: SuggestRequest):
    if not (body.goal or "").strip():
        return {"error": "Describe your goal."}
    key = os.getenv("ANTHROPIC_API_KEY", "")
    import agents_api
    roster = agents_api._load_all()
    # compact roster the planner can choose from
    catalog = "\n".join(f"- {a['slug']} ({a['division']}): {a['role']}" for a in roster.values())
    if not key:
        # heuristic fallback: pick a couple of marketing agents
        picks = [s for s in ("growth-hacker", "paid-media-auditor", "content-creator") if s in roster][:3]
        return {"ok": True, "name": body.goal[:60],
                "steps": [{"agent_slug": s, "task": f"Contribute your part toward: {body.goal}"} for s in picks],
                "model": "offline"}
    system = (
        "You assemble a small TEAM of agents into an ordered sequence to accomplish a goal. "
        "Choose 2-5 agents from the roster whose roles fit, ordered so each builds on the previous. "
        "Respond with ONLY JSON: {\"name\": str, \"steps\": [{\"agent_slug\": str, \"task\": str}]}. "
        "agent_slug MUST be an exact slug from the roster. Each task is a concrete instruction for that agent.")
    try:
        import anthropic
        client = system_llm.anthropic_client(api_key=key)
        model = os.getenv("JARVIS_AGENT_MODEL", "claude-sonnet-4-6")
        resp = await client.messages.create(
            model=model, max_tokens=800, system=system,
            messages=[{"role": "user", "content": f"Goal: {body.goal}\n\nRoster:\n{catalog[:9000]}"}])
        text = resp.content[0].text if resp.content else ""
        m = re.search(r"\{.*\}", text, re.DOTALL)
        spec = json.loads(m.group(0)) if m else {}
    except Exception as e:
        return {"error": f"Suggestion failed: {e}"}
    # keep only valid slugs
    steps = [s for s in (spec.get("steps") or []) if s.get("agent_slug") in roster][:6]
    if not steps:
        return {"error": "Couldn't map that goal to agents — try rephrasing."}
    return {"ok": True, "name": spec.get("name") or body.goal[:60], "steps": steps}


_WF_KNOWN_TYPES = {
    "trigger", "manualTrigger", "webhookTrigger", "formTrigger", "errorTrigger", "chatTrigger",
    "set", "transform", "code", "if", "switch", "filter", "merge", "loop", "noop",
    "stopAndError", "wait", "sort", "limit", "removeDuplicates", "aggregate",
    "splitOut", "renameKeys", "dateTime", "html", "extractFile", "http",
    "slack", "teams", "discord", "airtable", "mcp", "agent", "analysis",
    "respond", "subworkflow", "email", "webhook", "note", "approval",
    "gmail", "gsheets", "gcalendar", "gdrive", "notion", "hubspot", "stripe", "github",
    "airbyte",
}

_GRAPH_NODE_CATALOG = """
manualTrigger — starts the flow (params: pinned = JSON test data)
webhookTrigger — HTTP entrypoint
formTrigger — public form entry (params: fields = "a,b,c")
set — set output; params: value (template) OR fields {k:v}
transform — AI rewrites input; params: instruction
code — Python; params: code (uses `items`, sets `result`)
if — branch; params: left, op (contains/equals/gt/lt/is_empty…), right; OUT ports: true,false
switch — multi-branch; params: rules [str]; OUT ports: case-0…,default
filter — gate; params: left, op, right
merge — combine inputs
loop — iterate items; OUT ports: loop,done
sort — params: field, order (asc/desc)
limit — params: count, keep (first/last)
removeDuplicates — params: field
aggregate — params: field, op (list/count/sum/avg/concat), sep
splitOut — params: field (array)
renameKeys — params: map {old:new}
dateTime — params: field, format
html — params: mode (text/extract), tag
extractFile — params: format (csv/json)
http — call API; params: method, url, headers, body (top-level url)
slack/teams/discord — params: credential_id, message
airtable — params: credential_id, base_id, table, action, fields
mcp — call a connector tool; params: server_id, tool, arguments
agent — an AI agent; fields: agent_slug, task
analysis — params: tool (explain_metric/forecast_metric) + table/metric_field/date_field
respond — return a value; params: value
subworkflow — run another workflow; params: workflow_id
"""


class BuildGraphRequest(BaseModel):
    goal: str


@router.post("/build_graph")
async def build_graph(body: BuildGraphRequest):
    if not (body.goal or "").strip():
        return {"error": "Describe the automation you want."}
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return {"error": "AI is not configured (set ANTHROPIC_API_KEY)."}
    import agents_api
    roster = agents_api._load_all()
    slugs = ", ".join(list(roster.keys())[:120])
    system = (
        "You design an automation as a node graph for an n8n-style engine. "
        "Return ONLY JSON: {\"name\": str, \"nodes\": [{\"id\": str, \"type\": str, \"title\": str, "
        "\"agent_slug\": str?, \"params\": {..}}], \"edges\": [{\"from\": id, \"to\": id, \"branch\": str?}]}. "
        "Rules: every flow starts with a trigger node. Use ids like n1,n2,… Reference upstream data with "
        "expressions such as {{$json.field}} or {{upstream}} inside params. For if/switch/loop use the named "
        "OUT ports on edges via \"branch\" (true/false, case-0, loop/done). Only use node types from the catalog. "
        "For agent nodes set agent_slug to an EXACT slug from the provided list. Keep it to 3-8 nodes.\n\n"
        "NODE CATALOG:\n" + _GRAPH_NODE_CATALOG + "\nAGENT SLUGS: " + slugs)
    try:
        import anthropic
        client = system_llm.anthropic_client(api_key=key)
        model = os.getenv("JARVIS_AGENT_MODEL", "claude-sonnet-4-6")
        resp = await client.messages.create(
            model=model, max_tokens=1600, system=system,
            messages=[{"role": "user", "content": f"Automation to build: {body.goal}"}])
        text = resp.content[0].text if resp.content else ""
        m = re.search(r"\{.*\}", text, re.DOTALL)
        spec = json.loads(m.group(0)) if m else {}
    except Exception as e:
        return {"error": f"Build failed: {e}"}
    nodes = spec.get("nodes") or []
    if not nodes:
        return {"error": "Couldn't design that — try rephrasing."}
    # validate + lay out on a grid; drop unknown node types / bad agent slugs
    valid_types = set(_WF_KNOWN_TYPES)
    clean, idset = [], set()
    for i, n in enumerate(nodes):
        t = n.get("type")
        if t not in valid_types:
            continue
        nid = n.get("id") or f"n{i+1}"
        node = {"id": nid, "type": t, "title": n.get("title") or t,
                "x": 80 + (i % 3) * 240, "y": 60 + (i // 3) * 150,
                "params": n.get("params") or {}}
        if t == "agent":
            node["agent_slug"] = n.get("agent_slug") if n.get("agent_slug") in roster else ""
        if t == "http":
            node["url"] = (n.get("params") or {}).get("url", "")
        clean.append(node); idset.add(nid)
    edges = [e for e in (spec.get("edges") or []) if e.get("from") in idset and e.get("to") in idset]
    return {"ok": True, "name": spec.get("name") or body.goal[:60], "nodes": clean, "edges": edges}


# ── Scheduling ───────────────────────────────────────────────────────────────

class ScheduleRequest(BaseModel):
    cron: Optional[str] = None


@router.post("/{wid}/schedule")
async def schedule_workflow(wid: str, body: ScheduleRequest):
    data = _load(_WF_PATH, "workflows")
    w = next((x for x in data.get("workflows", []) if x["id"] == wid), None)
    if not w:
        return {"error": "Not found."}
    w["schedule"] = (body.cron or "").strip() or None
    _save(_WF_PATH, data)
    return {"ok": True, "schedule": w.get("schedule")}


@router.get("/runs")
async def list_runs():
    return {"ok": True, "runs": _load(_RUN_PATH, "runs").get("runs", [])}


def _versions_map():
    vm = _load(_VERS_PATH, "versions").get("versions")
    return vm if isinstance(vm, dict) else {}


@router.get("/{wid}/versions")
async def list_versions(wid: str):
    lst = _versions_map().get(wid, [])
    return {"ok": True, "versions": [{"vid": v["vid"], "at": v["at"], "name": v.get("name"),
             "node_count": len((v.get("snapshot") or {}).get("nodes") or [])} for v in lst]}


class ActivateRequest(BaseModel):
    active: bool


@router.post("/{wid}/activate")
async def activate_workflow(wid: str, body: ActivateRequest):
    data = _load(_WF_PATH, "workflows")
    w = next((x for x in data.get("workflows", []) if x["id"] == wid), None)
    if not w:
        return {"error": "Workflow not found."}
    w["active"] = bool(body.active)
    _save(_WF_PATH, data)
    return {"ok": True, "active": w["active"]}


@router.get("/overview")
async def overview():
    wfs = _load(_WF_PATH, "workflows").get("workflows", [])
    runs = _load(_RUN_PATH, "runs").get("runs", [])
    last_status = {}
    for r in runs:  # runs are newest-first
        wid = r.get("workflow_id")
        if wid and wid not in last_status:
            last_status[wid] = {"failed": bool(r.get("failed")), "ran_at": r.get("ran_at")}
    out = []
    for w in wfs:
        out.append({"id": w.get("id"), "name": w.get("name"),
                    "schedule": w.get("schedule"), "active": w.get("active", True),
                    "node_count": len(w.get("nodes") or []),
                    "last_run": w.get("last_run"),
                    "last_status": last_status.get(w.get("id"))})
    return {"ok": True, "workflows": out}


@router.post("/{wid}/restore/{vid}")
async def restore_version(wid: str, vid: str):
    lst = _versions_map().get(wid, [])
    ver = next((v for v in lst if v["vid"] == vid), None)
    if not ver:
        return {"error": "Version not found."}
    data = _load(_WF_PATH, "workflows")
    w = next((x for x in data.get("workflows", []) if x["id"] == wid), None)
    if not w:
        return {"error": "Workflow not found."}
    _snapshot_version(w)  # snapshot current before rolling back
    snap = ver.get("snapshot") or {}
    for k in ("name", "context", "recipients", "nodes", "edges", "notes", "steps", "alert_webhook", "error_workflow_id", "schedule"):
        if k in snap:
            w[k] = snap[k]
    w["updated_at"] = int(time.time())
    _save(_WF_PATH, data)
    return {"ok": True, "workflow": w}


# ── Human-in-the-loop approvals ──────────────────────────────────────────────
def _descendants(node_id, edges, idmap):
    seen, stack = set(), [node_id]
    while stack:
        cur = stack.pop()
        for e in edges:
            if e.get("from") == cur and e.get("to") in idmap and e["to"] not in seen:
                seen.add(e["to"]); stack.append(e["to"])
    return seen


def _persist_approvals(wid, name, snapshot, pending):
    """Save pending approvals from a paused run and notify the approver."""
    store = _load(_APPROVAL_PATH, "approvals")
    lst = store.setdefault("approvals", [])
    created = []
    for pnd in pending:
        aid = uuid.uuid4().hex[:12]
        rec = {"id": aid, "workflow_id": wid, "workflow_name": name,
               "node_id": pnd["node_id"], "title": pnd["title"], "message": pnd.get("message", ""),
               "created_at": int(time.time()), "status": "pending",
               "items": pnd.get("items", []), "snapshot": snapshot}
        lst.insert(0, rec)
        created.append(rec)
    del lst[100:]
    _save(_APPROVAL_PATH, store)
    return created


async def _notify_approval(w, rec):
    subject = f"Approval needed: {rec.get('workflow_name','workflow')} — {rec.get('title','step')}"
    body = (f"A workflow step is waiting for your approval.\n\n"
            f"Workflow: {rec.get('workflow_name')}\nStep: {rec.get('title')}\n"
            f"{rec.get('message','')}\n\nApprove or reject it in Agent Studio → Approvals.")
    recips = (w.get("recipients") or "").strip() if w else ""
    if recips:
        try:
            import smtp_mailer, asyncio as _a
            if smtp_mailer.is_configured():
                await _a.get_running_loop().run_in_executor(None, lambda: smtp_mailer.send(recips, subject, body, None))
        except Exception as e:
            log.debug("approval email skipped: %s", e)
    url = ((w or {}).get("alert_webhook") or "").strip()
    if url.startswith("http"):
        try:
            await _post_json(url, {"type": "approval_request", "workflow": rec.get("workflow_name"),
                                   "step": rec.get("title"), "message": rec.get("message")})
        except Exception as e:
            log.debug("approval webhook skipped: %s", e)


@router.get("/approvals")
async def list_approvals():
    lst = _load(_APPROVAL_PATH, "approvals").get("approvals", [])
    pend = [{"id": a["id"], "workflow_id": a.get("workflow_id"), "workflow_name": a.get("workflow_name"),
             "title": a.get("title"), "message": a.get("message"), "created_at": a.get("created_at"),
             "status": a.get("status")} for a in lst if a.get("status") == "pending"]
    return {"ok": True, "approvals": pend}


class ApprovalDecision(BaseModel):
    approve: bool
    note: Optional[str] = ""


@router.post("/approvals/{aid}/decide")
async def decide_approval(aid: str, body: ApprovalDecision):
    store = _load(_APPROVAL_PATH, "approvals")
    rec = next((a for a in store.get("approvals", []) if a.get("id") == aid), None)
    if not rec:
        return {"error": "Approval not found."}
    if rec.get("status") != "pending":
        return {"error": f"Already {rec.get('status')}."}
    decision = "approved" if body.approve else "rejected"
    rec["status"] = decision
    rec["decided_at"] = int(time.time())
    rec["note"] = body.note or ""
    _save(_APPROVAL_PATH, store)
    # Resume: run the approval node + its descendants, seeded with the stored items,
    # injecting the decision so the correct branch runs (upstream is NOT re-run).
    snap = rec.get("snapshot") or {}
    all_nodes = snap.get("nodes", []); all_edges = snap.get("edges", [])
    idmap = {n["id"]: n for n in all_nodes if n.get("id")}
    keep = {rec["node_id"]} | _descendants(rec["node_id"], all_edges, idmap)
    sub_nodes = [idmap[k] for k in keep if k in idmap]
    sub_edges = [e for e in all_edges if e.get("from") in keep and e.get("to") in keep]
    res = await _run_graph(sub_nodes, sub_edges, snap.get("context", ""),
                           seed_items=rec.get("items", []),
                           decisions={rec["node_id"]: decision})
    try:
        _record_graph_run(rec.get("workflow_id"), (rec.get("workflow_name") or "Approved run"), res)
    except Exception:
        pass
    return {"ok": True, "decision": decision, "result": res}


# ── Workflow loader + webhook / form triggers ────────────────────────────────
def _load_workflow(wid):
    if not wid:
        return None
    for w in _load(_WF_PATH, "workflows").get("workflows", []):
        if w.get("id") == wid:
            return w
    return None


def _workflow_by_token(token):
    for w in _load(_WF_PATH, "workflows").get("workflows", []):
        if w.get("webhook_token") and w.get("webhook_token") == token:
            return w
    return None


@router.post("/{wid}/enable_webhook")
async def enable_webhook(wid: str):
    data = _load(_WF_PATH, "workflows")
    for w in data.get("workflows", []):
        if w.get("id") == wid:
            w["webhook_token"] = w.get("webhook_token") or uuid.uuid4().hex
            _save(_WF_PATH, data)
            return {"ok": True, "token": w["webhook_token"], "path": f"/api/workflows/hook/{w['webhook_token']}"}
    return {"error": "Workflow not found."}


async def _run_workflow_seeded(w, seed_items):
    res = await _run_graph(w.get("nodes", []), w.get("edges", []), w.get("context", ""),
                           seed_items=seed_items, return_items=True)
    try:
        _record_graph_run(w.get("id"), w.get("name", "Triggered run"), res)
    except Exception:
        pass
    resp = next((nn for nn in res.get("nodes", []) if nn.get("respond")), None)
    if resp:
        return resp.get("output", "")
    agg = []
    for tnid in res.get("terminals", []):
        agg.extend(res.get("item_map", {}).get(tnid, []))
    return _items_to_text(agg) if agg else "ok"


@router.post("/hook/{token}")
async def run_hook(token: str, request: Request):
    w = _workflow_by_token(token)
    if not w:
        return JSONResponse({"error": "Unknown webhook."}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if isinstance(body, dict):
        seed = [body]
    elif isinstance(body, list):
        seed = body
    else:
        seed = [{"text": str(body)}]
    out = await _run_workflow_seeded(w, seed)
    return JSONResponse({"ok": True, "result": out})


@router.get("/form/{token}", response_class=HTMLResponse)
async def form_page(token: str):
    w = _workflow_by_token(token)
    if not w:
        return HTMLResponse("<h3>Unknown form.</h3>", status_code=404)
    trig = next((n for n in w.get("nodes", []) if n.get("type") == "formTrigger"), None)
    fields = ((trig or {}).get("params") or {}).get("fields") or ["message"]
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",") if f.strip()]
    inputs = "".join(
        f'<label style="display:block;margin:10px 0 4px;font-weight:600;">{f}</label>'
        f'<input name="{f}" style="width:100%;padding:9px;border:1px solid #cbd5e1;border-radius:8px;">'
        for f in fields)
    html = f"""<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>{w.get('name','Form')}</title></head>
<body style="font-family:system-ui;max-width:520px;margin:40px auto;padding:0 16px;">
<h2>{w.get('name','Form')}</h2>
<form id=f>{inputs}
<button style="margin-top:16px;padding:10px 18px;background:#0E76B4;color:#fff;border:0;border-radius:8px;cursor:pointer;">Submit</button>
</form><div id=msg style="margin-top:14px;"></div>
<script>
document.getElementById('f').addEventListener('submit',async e=>{{e.preventDefault();
const fd=new FormData(e.target);const o={{}};fd.forEach((v,k)=>o[k]=v);
const r=await fetch('/api/workflows/hook/{token}',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(o)}});
const j=await r.json();document.getElementById('msg').textContent=j.ok?'Thanks — submitted.':'Something went wrong.';e.target.reset();}});
</script></body></html>"""
    return HTMLResponse(html)


# ── Credentials manager ──────────────────────────────────────────────────────
def _get_cred(cid):
    if not cid:
        return None
    for c in _load(_CRED_PATH, "credentials").get("credentials", []):
        if c.get("id") == cid:
            c = dict(c)  # decrypt at point of use; stored copy stays encrypted
            if crypto_store:
                c["data"] = crypto_store.reveal_dict(c.get("data") or {})
            return c
    return None


def _mask(v):
    s = str(v or "")
    if len(s) <= 6:
        return "••••"
    return s[:3] + "…" + s[-3:]


def _cred_preview(c):
    d = c.get("data") or {}
    if crypto_store:
        d = crypto_store.reveal_dict(d)
    keys = list(d.keys())
    return _mask(d.get(keys[0], "")) if keys else ""


@router.get("/credentials")
async def list_credentials():
    creds = _load(_CRED_PATH, "credentials").get("credentials", [])
    safe = [{"id": c.get("id"), "name": c.get("name"), "type": c.get("type"),
             "preview": _cred_preview(c)} for c in creds]
    return {"ok": True, "credentials": safe, "types": _CRED_TYPES}


class CredentialRequest(BaseModel):
    id: Optional[str] = None
    name: str
    type: str
    data: dict = {}


@router.post("/credentials")
async def save_credential(body: CredentialRequest):
    if body.type not in _CRED_TYPES:
        return {"error": f"Unknown credential type '{body.type}'."}
    store = _load(_CRED_PATH, "credentials")
    creds = store.setdefault("credentials", [])
    if body.id:
        for c in creds:
            if c.get("id") == body.id:
                c["name"] = body.name; c["type"] = body.type
                # keep existing secret values when the field is left blank (edit without re-typing)
                merged = dict(c.get("data") or {})
                for k, v in (body.data or {}).items():
                    if str(v).strip():
                        merged[k] = crypto_store.protect(v) if crypto_store else v  # encrypt at rest
                c["data"] = merged
                _save(_CRED_PATH, store)
                return {"ok": True, "id": body.id}
    cid = uuid.uuid4().hex[:12]
    data = crypto_store.protect_dict(body.data or {}) if crypto_store else (body.data or {})
    creds.append({"id": cid, "name": body.name, "type": body.type, "data": data})
    _save(_CRED_PATH, store)
    return {"ok": True, "id": cid}


class MemoryTestRequest(BaseModel):
    credential_id: Optional[str] = None


@router.post("/memory/test")
async def memory_test(body: MemoryTestRequest):
    """Probe an agent's Postgres memory backend (used by the inspector Test button)."""
    import agent_memory
    cred = _get_cred(body.credential_id)
    dsn = agent_memory.dsn_from_cred(cred.get("data")) if cred else ""
    return agent_memory.test_connection(dsn)


# ── n8n parity: import a cURL command into an HTTP node ─────────────────────
def parse_curl(curl: str) -> dict:
    """Parse a cURL command into {method, url, headers, body, params}. Mirrors
    n8n's 'Import cURL' — paste a curl and get a configured HTTP node."""
    import shlex
    s = (curl or "").strip()
    if s.startswith("curl "):
        s = s[5:]
    s = s.replace("\\\n", " ").replace("\\\r\n", " ")
    try:
        toks = shlex.split(s)
    except Exception:
        toks = s.split()
    method, url, body = "", "", ""
    headers = {}
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("-X", "--request") and i + 1 < len(toks):
            method = toks[i + 1].upper(); i += 2; continue
        if t in ("-H", "--header") and i + 1 < len(toks):
            hv = toks[i + 1]
            if ":" in hv:
                k, v = hv.split(":", 1); headers[k.strip()] = v.strip()
            i += 2; continue
        if t in ("-d", "--data", "--data-raw", "--data-binary", "--data-urlencode") and i + 1 < len(toks):
            body = (body + "&" + toks[i + 1]) if body else toks[i + 1]; i += 2; continue
        if t in ("-u", "--user") and i + 1 < len(toks):
            import base64 as _b64
            headers["Authorization"] = "Basic " + _b64.b64encode(toks[i + 1].encode()).decode(); i += 2; continue
        if t in ("--url",) and i + 1 < len(toks):
            url = toks[i + 1]; i += 2; continue
        if t in ("-A", "--user-agent") and i + 1 < len(toks):
            headers["User-Agent"] = toks[i + 1]; i += 2; continue
        if t in ("--compressed", "-L", "--location", "-s", "--silent", "-k", "--insecure", "-i", "-v"):
            i += 1; continue
        if t.startswith("http://") or t.startswith("https://"):
            url = t; i += 1; continue
        if not t.startswith("-") and not url:
            url = t
        i += 1
    if not method:
        method = "POST" if body else "GET"
    return {"method": method, "url": url, "headers": headers, "body": body}


class CurlBody(BaseModel):
    curl: str


@router.post("/parse_curl")
async def parse_curl_route(body: CurlBody):
    r = parse_curl(body.curl)
    if not r.get("url"):
        return {"error": "Could not find a URL in that cURL command."}
    return {"ok": True, **r}


# ── n8n parity: AI-assisted error analysis of a failed node/run ─────────────
class ExplainErrorBody(BaseModel):
    error: str
    node_type: Optional[str] = ""
    node_title: Optional[str] = ""
    context: Optional[str] = ""


@router.post("/explain_error")
async def explain_error(body: ExplainErrorBody):
    """AI-assisted error analysis: explain a workflow error in plain language and
    suggest a concrete fix (n8n's 'AI-assisted error analysis')."""
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return {"explanation": "", "error": "AI not configured (ANTHROPIC_API_KEY not set)."}
    import llm_router
    prov = {"type": "anthropic", "api_key": key,
            "model": os.getenv("JARVIS_AGENT_MODEL", "claude-sonnet-4-6"), "name": "Claude"}
    sysp = ("You are a workflow-automation debugging assistant. Given a node error, explain in plain language what "
            "likely went wrong and give 1-3 concrete, specific fixes (config, credentials, data mapping, expression "
            "syntax). Be concise. Format: a one-line summary, then a short bulleted list of fixes.")
    msg = (f"Node type: {body.node_type}\nNode: {body.node_title}\nError:\n{body.error}\n\n"
           f"Upstream/context:\n{(body.context or '')[:1500]}")
    r = await llm_router.complete(prov, sysp, [{"role": "user", "content": msg}], max_tokens=500)
    return {"explanation": r.get("output", ""), "error": r.get("error")}


# ── n8n parity: external secrets ({{$secrets.NAME}}) ────────────────────────
_SECRETS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflow_secrets.json")
_SECRET_PROVIDERS = ["value", "env", "hashicorp_vault", "aws_secrets_manager",
                     "azure_key_vault", "gcp_secret_manager", "infisical"]


def _secrets_store():
    return _load(_SECRETS_PATH, "secrets").get("secrets", [])


def _secret_rec(name):
    for s in _secrets_store():
        if s.get("name") == name:
            return s
    return None


def _resolve_vault(rec):
    """Fetch a secret from an external vault. HashiCorp Vault is implemented over its
    HTTP API (VAULT_ADDR + VAULT_TOKEN). Cloud providers (AWS/Azure/GCP/Infisical) fall
    back to an environment variable until their SDK/credentials are wired — the secret
    value is never persisted here (n8n's 'external secrets' pattern)."""
    prov = rec.get("provider") or ""
    ref = rec.get("ref") or rec.get("name") or ""
    if prov == "hashicorp_vault":
        addr = os.getenv("VAULT_ADDR", ""); tok = os.getenv("VAULT_TOKEN", "")
        if addr and tok and ref:
            import urllib.request as _u, json as _j
            try:
                req = _u.Request(addr.rstrip("/") + "/v1/" + ref.lstrip("/"),
                                 headers={"X-Vault-Token": tok})
                with _u.urlopen(req, timeout=10) as resp:
                    d = _j.loads(resp.read().decode("utf-8", "replace"))
                data = (d.get("data") or {}).get("data") or d.get("data") or {}
                key = rec.get("key")
                if key:
                    return str(data.get(key, ""))
                return str(next(iter(data.values()), "")) if isinstance(data, dict) and data else ""
            except Exception as e:
                log.warning(f"vault fetch failed: {e}")
                return ""
    # cloud providers: env fallback (real SDK integration is a deployment concern)
    return os.getenv(ref or rec.get("name", ""), "")


def _get_secret(name):
    """Resolve a named secret for {{$secrets.NAME}}. Never returned to the client via
    the list endpoint — only used server-side during a run."""
    rec = _secret_rec(name)
    if not rec:
        return os.getenv(name, "")
    src = rec.get("source", "value")
    if src == "value":
        v = rec.get("value", "") or ""
        return str(crypto_store.reveal(v) if crypto_store else v)
    if src == "env":
        return os.getenv(rec.get("env") or name, "")
    if src == "vault":
        return _resolve_vault(rec)
    return ""


class SecretBody(BaseModel):
    name: str
    source: str = "value"      # value | env | vault
    value: Optional[str] = ""  # for source=value (write-only; never returned)
    env: Optional[str] = ""    # for source=env: the env var name
    provider: Optional[str] = ""  # for source=vault
    ref: Optional[str] = ""    # vault path / secret id
    key: Optional[str] = ""    # key within a vault secret


@router.get("/secrets")
async def list_secrets():
    out = [{"name": s.get("name"), "source": s.get("source", "value"),
            "provider": s.get("provider") or "",
            "set": bool(s.get("value") or s.get("env") or s.get("ref"))}
           for s in _secrets_store()]
    return {"secrets": out, "providers": _SECRET_PROVIDERS}


@router.post("/secrets")
async def save_secret(body: SecretBody):
    name = (body.name or "").strip()
    if not name or not re.match(r'^[A-Za-z0-9_\-]+$', name):
        return {"error": "Name must be alphanumeric (letters, digits, _ or -)."}
    store = _load(_SECRETS_PATH, "secrets")
    secrets = store.setdefault("secrets", [])
    val = body.value or ""
    if val and crypto_store:
        val = crypto_store.protect(val)   # encrypt the stored secret value at rest
    d = {"name": name, "source": body.source, "value": val,
         "env": body.env or "", "provider": body.provider or "", "ref": body.ref or "", "key": body.key or ""}
    for i, s in enumerate(secrets):
        if s.get("name") == name:
            if not (body.value or "").strip() and s.get("value"):
                d["value"] = s["value"]   # keep existing (already-encrypted) value on edit-without-retype
            secrets[i] = d
            _save(_SECRETS_PATH, store)
            return {"ok": True, "name": name}
    secrets.append(d)
    _save(_SECRETS_PATH, store)
    return {"ok": True, "name": name}


@router.delete("/secrets/{name}")
async def delete_secret(name: str):
    store = _load(_SECRETS_PATH, "secrets")
    store["secrets"] = [s for s in store.get("secrets", []) if s.get("name") != name]
    _save(_SECRETS_PATH, store)
    return {"ok": True}


# ── n8n parity: workflow performance insights over time ─────────────────────
@router.get("/insights")
async def workflow_insights():
    """Aggregate recent run history into performance metrics (n8n's 'insights on
    workflow performance over time')."""
    runs = _load(_RUN_PATH, "runs").get("runs", [])
    total = len(runs)
    failed = sum(1 for r in runs if r.get("failed"))
    steps_vals = [r.get("step_count") or len(r.get("nodes", [])) for r in runs]
    avg_steps = round(sum(steps_vals) / total, 1) if total else 0

    def _day(ts):
        try:
            return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
        except Exception:
            return "—"
    by_day = {}
    for r in runs:
        d = _day(r.get("ran_at") or time.time())
        b = by_day.setdefault(d, {"runs": 0, "failed": 0})
        b["runs"] += 1
        b["failed"] += 1 if r.get("failed") else 0
    per_wf = {}
    for r in runs:
        k = r.get("workflow_id") or r.get("name") or "ad-hoc"
        w = per_wf.setdefault(k, {"name": r.get("name") or k, "runs": 0, "failed": 0, "last_run": 0})
        w["runs"] += 1
        w["failed"] += 1 if r.get("failed") else 0
        w["last_run"] = max(w["last_run"], float(r.get("ran_at") or 0))
    return {
        "total_runs": total, "failed_runs": failed,
        "success_rate": round((total - failed) / total * 100, 1) if total else 100.0,
        "avg_steps": avg_steps,
        "by_day": [{"day": k, **by_day[k]} for k in sorted(by_day)],
        "by_workflow": sorted(per_wf.values(), key=lambda x: -x["runs"]),
        "window": "last %d runs" % total,
    }


# ── n8n parity: environments (draft → production) with one-click promote ────
@router.post("/{wid}/promote")
async def promote_workflow(wid: str):
    """Promote the current draft to the Production environment — a frozen snapshot
    that scheduled/production runs use, independent of further editing."""
    data = _load(_WF_PATH, "workflows")
    w = next((x for x in data.get("workflows", []) if x["id"] == wid), None)
    if not w:
        return {"error": "Not found."}
    w["production"] = {"nodes": w.get("nodes", []), "edges": w.get("edges", []),
                       "notes": w.get("notes", []), "promoted_at": time.time()}
    _save(_WF_PATH, data)
    return {"ok": True, "promoted_at": w["production"]["promoted_at"]}


@router.get("/{wid}/environments")
async def workflow_environments(wid: str):
    """Report environment status: whether production exists and whether the draft has
    diverged since the last promotion."""
    w = _load_workflow(wid)
    if not w:
        return {"error": "Not found."}
    prod = w.get("production")
    diverged = False
    if prod:
        diverged = (json.dumps(prod.get("nodes"), sort_keys=True, default=str) !=
                    json.dumps(w.get("nodes"), sort_keys=True, default=str) or
                    json.dumps(prod.get("edges"), sort_keys=True, default=str) !=
                    json.dumps(w.get("edges"), sort_keys=True, default=str))
    return {"has_production": bool(prod),
            "promoted_at": (prod or {}).get("promoted_at"),
            "draft_diverged": diverged}


# ── n8n parity: projects + roles (team access to groups of workflows) ───────
_PROJECT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflow_projects.json")
_PROJECT_ROLES = ["owner", "editor", "viewer"]


class ProjectBody(BaseModel):
    id: Optional[str] = None
    name: str
    members: Optional[list] = None  # [{email, role}]


@router.get("/projects")
async def list_projects():
    projs = _load(_PROJECT_PATH, "projects").get("projects", [])
    wfs = _load(_WF_PATH, "workflows").get("workflows", [])
    for p in projs:
        p["workflow_count"] = sum(1 for w in wfs if w.get("project_id") == p["id"])
    return {"projects": projs, "roles": _PROJECT_ROLES}


@router.post("/projects")
async def save_project(body: ProjectBody):
    if not (body.name or "").strip():
        return {"error": "Project name is required."}
    store = _load(_PROJECT_PATH, "projects")
    projs = store.setdefault("projects", [])
    members = [{"email": (m.get("email") or "").strip(), "role": m.get("role") or "viewer"}
               for m in (body.members or []) if (m.get("email") or "").strip()]
    if body.id:
        for p in projs:
            if p["id"] == body.id:
                p["name"] = body.name.strip(); p["members"] = members
                _save(_PROJECT_PATH, store)
                return {"ok": True, "id": body.id}
    pid = "prj_" + uuid.uuid4().hex[:10]
    projs.append({"id": pid, "name": body.name.strip(), "members": members, "created": time.time()})
    _save(_PROJECT_PATH, store)
    return {"ok": True, "id": pid}


@router.delete("/projects/{pid}")
async def delete_project(pid: str):
    store = _load(_PROJECT_PATH, "projects")
    store["projects"] = [p for p in store.get("projects", []) if p.get("id") != pid]
    _save(_PROJECT_PATH, store)
    # unassign workflows
    wdata = _load(_WF_PATH, "workflows")
    for w in wdata.get("workflows", []):
        if w.get("project_id") == pid:
            w["project_id"] = ""
    _save(_WF_PATH, wdata)
    return {"ok": True}


class AssignProjectBody(BaseModel):
    workflow_id: str
    project_id: str = ""


@router.post("/projects/assign")
async def assign_project(body: AssignProjectBody):
    data = _load(_WF_PATH, "workflows")
    w = next((x for x in data.get("workflows", []) if x["id"] == body.workflow_id), None)
    if not w:
        return {"error": "Workflow not found."}
    w["project_id"] = body.project_id or ""
    _save(_WF_PATH, data)
    return {"ok": True}


# ── Compliance self-check: live readiness signals for SOC 2 / HIPAA ─────────
@router.get("/compliance/status")
async def compliance_status():
    """A live readiness checklist for the technical safeguards this app controls.
    Organizational controls (BAAs, policies, audit) are tracked outside the app."""
    enc_ok = bool(crypto_store and crypto_store.available())
    key_src = crypto_store.key_source() if crypto_store else "none"
    # count how many stored secrets/credentials are actually encrypted at rest
    creds = _load(_CRED_PATH, "credentials").get("credentials", [])
    cred_vals = [v for c in creds for v in (c.get("data") or {}).values() if isinstance(v, str)]
    enc_creds = sum(1 for v in cred_vals if crypto_store and crypto_store.is_encrypted(v))
    secs = [s for s in _load(_SECRETS_PATH, "secrets").get("secrets", []) if s.get("source") == "value"]
    enc_secs = sum(1 for s in secs if crypto_store and crypto_store.is_encrypted(s.get("value") or ""))
    checks = [
        {"control": "Encryption at rest (credentials/secrets)", "framework": "HIPAA §164.312(a)(2)(iv) · SOC 2 C",
         "status": "pass" if enc_ok else "fail",
         "detail": (f"Encryption active ({enc_creds}/{len(cred_vals)} credential values, "
                    f"{enc_secs}/{len(secs)} value-secrets encrypted). Key source: {key_src}."
                    if enc_ok else "cryptography not available — values stored in plaintext. Install `cryptography`.")},
        {"control": "Key management", "framework": "SOC 2 Security",
         "status": "pass" if key_src == "env" else ("warn" if key_src == "file" else "fail"),
         "detail": {"env": "Key supplied from environment/KMS (good).",
                    "file": "Dev key file in use — supply JARVIS_SECRET_KEY from your KMS for production.",
                    "none": "No key configured."}.get(key_src, "")},
        {"control": "PHI de-identification pipeline", "framework": "HIPAA §164.514",
         "status": "pass",
         "detail": "Datavant tokenize → de-identify (Safe Harbor / Expert Determination) → certify is available."},
        {"control": "On-prem / no-egress LLM option", "framework": "HIPAA transmission security",
         "status": "pass", "detail": "Local Ollama provider keeps PHI on-network; de-identify before any cloud LLM."},
        {"control": "Access control + roles", "framework": "HIPAA §164.312(a)(1) · SOC 2 Security",
         "status": "pass", "detail": "Login + signed sessions, Admin security governance, projects/roles."},
        {"control": "Secrets never returned to client", "framework": "SOC 2 Confidentiality",
         "status": "pass", "detail": "Secret values are write-only; only names/metadata are listed."},
        {"control": "Audit logging (comprehensive PHI access log)", "framework": "HIPAA §164.312(b)",
         "status": "warn", "detail": "Marketplace/licenses are audited; a centralized append-only access log is the next build."},
        {"control": "MFA on login", "framework": "SOC 2 Security",
         "status": "warn", "detail": "Password login present; TOTP MFA is a planned control."},
        {"control": "Automatic logoff / session timeout", "framework": "HIPAA §164.312(a)(2)(iii)",
         "status": "warn", "detail": "Signed sessions present; idle-timeout enforcement is a planned control."},
        {"control": "TLS in transit (valid cert)", "framework": "HIPAA transmission security",
         "status": "warn", "detail": "HTTPS enabled; production requires a CA-signed cert, not self-signed."},
        {"control": "Signed BAAs with subprocessors", "framework": "HIPAA §164.308(b)",
         "status": "manual", "detail": "Organizational: Anthropic (Messages API is BAA-eligible), Airbyte, Datavant, TTS, hosting."},
        {"control": "SOC 2 Type II audit (independent)", "framework": "SOC 2",
         "status": "manual", "detail": "Organizational: engage a CPA auditor + evidence over a 3–12 month window."},
    ]
    score = sum(1 for c in checks if c["status"] == "pass")
    return {"checks": checks, "passing": score, "total": len(checks),
            "encryption_available": enc_ok, "key_source": key_src,
            "note": "Technical safeguards are self-reported; certification requires an independent audit."}


@router.delete("/credentials/{cid}")
async def delete_credential(cid: str):
    store = _load(_CRED_PATH, "credentials")
    store["credentials"] = [c for c in store.get("credentials", []) if c.get("id") != cid]
    _save(_CRED_PATH, store)
    return {"ok": True}


# ── MCP servers (connectors) ─────────────────────────────────────────────────
def _get_mcp(sid):
    for s in _load(_MCP_PATH, "servers").get("servers", []):
        if s.get("id") == sid:
            return s
    return None


def _mcp_safe(s):
    """Public view — hide secret env/header values."""
    return {"id": s.get("id"), "name": s.get("name"), "transport": s.get("transport"),
            "command": s.get("command", ""), "args": s.get("args", []),
            "url": s.get("url", ""),
            "env_keys": list((s.get("env") or {}).keys()),
            "header_keys": list((s.get("headers") or {}).keys()),
            "tools": s.get("tools", [])}


@router.get("/mcp_servers")
async def list_mcp_servers():
    servers = _load(_MCP_PATH, "servers").get("servers", [])
    return {"ok": True, "servers": [_mcp_safe(s) for s in servers]}


class MCPServerRequest(BaseModel):
    id: Optional[str] = None
    name: str
    transport: str = "sse"              # stdio | sse | http
    command: Optional[str] = ""
    args: Optional[list] = None
    env: Optional[dict] = None
    url: Optional[str] = ""
    headers: Optional[dict] = None


@router.post("/mcp_servers")
async def save_mcp_server(body: MCPServerRequest):
    if not (body.name or "").strip():
        return {"error": "Name is required."}
    if body.transport == "stdio" and not (body.command or "").strip():
        return {"error": "A stdio server needs a command."}
    if body.transport in ("sse", "http") and not (body.url or "").strip():
        return {"error": "A remote server needs a URL."}
    store = _load(_MCP_PATH, "servers")
    servers = store.setdefault("servers", [])
    if body.id:
        for s in servers:
            if s.get("id") == body.id:
                s["name"] = body.name; s["transport"] = body.transport
                s["command"] = body.command or ""; s["args"] = body.args or []
                s["url"] = body.url or ""
                # merge secrets: keep existing values when a field is left blank
                for bucket, incoming in (("env", body.env), ("headers", body.headers)):
                    merged = dict(s.get(bucket) or {})
                    for k, v in (incoming or {}).items():
                        if str(v).strip():
                            merged[k] = v
                    s[bucket] = merged
                _save(_MCP_PATH, store)
                return {"ok": True, "id": body.id}
    sid = uuid.uuid4().hex[:12]
    servers.append({"id": sid, "name": body.name, "transport": body.transport,
                    "command": body.command or "", "args": body.args or [],
                    "env": body.env or {}, "url": body.url or "", "headers": body.headers or {},
                    "tools": []})
    _save(_MCP_PATH, store)
    return {"ok": True, "id": sid}


@router.delete("/mcp_servers/{sid}")
async def delete_mcp_server(sid: str):
    store = _load(_MCP_PATH, "servers")
    store["servers"] = [s for s in store.get("servers", []) if s.get("id") != sid]
    _save(_MCP_PATH, store)
    return {"ok": True}


# ── LLM providers (multi-model harness) ──────────────────────────────────────
@router.get("/llm_providers")
async def list_llm_providers():
    import llm_router
    store = _load(_LLM_PATH, "providers")
    provs = store.get("providers", [])
    active_id = store.get("active_id")
    safe = [{"id": p.get("id"), "name": p.get("name"), "type": p.get("type"),
             "model": p.get("model"), "base_url": p.get("base_url", ""),
             "local": llm_router.is_local(p), "has_key": bool(p.get("api_key")),
             "active": p.get("id") == active_id} for p in provs]
    return {"ok": True, "providers": safe, "active_id": active_id,
            "types": ["anthropic", "openai", "openai_compatible", "gemini", "ollama"]}


class LLMProviderRequest(BaseModel):
    id: Optional[str] = None
    name: str
    type: str = "openai"
    base_url: Optional[str] = ""
    api_key: Optional[str] = None
    model: str = ""


@router.post("/llm_providers")
async def save_llm_provider(body: LLMProviderRequest):
    if not (body.name or "").strip():
        return {"error": "Name is required."}
    store = _load(_LLM_PATH, "providers")
    provs = store.setdefault("providers", [])
    if body.id:
        for p in provs:
            if p.get("id") == body.id:
                p["name"] = body.name; p["type"] = body.type
                p["base_url"] = body.base_url or ""; p["model"] = body.model
                if body.api_key and str(body.api_key).strip():
                    p["api_key"] = body.api_key   # keep old key if left blank
                _save(_LLM_PATH, store)
                return {"ok": True, "id": body.id}
    pid = uuid.uuid4().hex[:12]
    provs.append({"id": pid, "name": body.name, "type": body.type,
                  "base_url": body.base_url or "", "api_key": body.api_key or "", "model": body.model})
    _save(_LLM_PATH, store)
    return {"ok": True, "id": pid}


@router.delete("/llm_providers/{pid}")
async def delete_llm_provider(pid: str):
    store = _load(_LLM_PATH, "providers")
    store["providers"] = [p for p in store.get("providers", []) if p.get("id") != pid]
    if store.get("active_id") == pid:
        store["active_id"] = None      # don't leave the system pointing at a deleted model
    _save(_LLM_PATH, store)
    return {"ok": True}


# ── Which model runs the SYSTEM (Martin + KTX data-question planner) ──────────
@router.get("/llm_active")
async def get_llm_active():
    """Report the model currently powering Martin + the data planner."""
    try:
        import system_llm
        system_llm.ensure_seed()
        return {"ok": True, "active": system_llm.active_summary()}
    except Exception as e:
        return {"error": f"Could not read active model: {e}"}


class LLMActiveRequest(BaseModel):
    id: Optional[str] = None   # provider id, or null / "" to fall back to the default Claude


@router.post("/llm_active")
async def set_llm_active(body: LLMActiveRequest):
    """Choose which configured provider runs the system. Pass id=null to reset to the Claude default."""
    try:
        import system_llm
        ok = system_llm.set_active((body.id or "").strip() or None)
        if not ok:
            return {"error": "That provider no longer exists."}
        return {"ok": True, "active": system_llm.active_summary()}
    except Exception as e:
        return {"error": f"Could not set active model: {e}"}


class LLMTestRequest(BaseModel):
    id: str


@router.post("/llm_test")
async def test_llm_provider(body: LLMTestRequest):
    """Send a tiny prompt to a provider so the admin can confirm it actually answers."""
    import llm_router
    prov = _get_llm_provider(body.id)
    if not prov:
        return {"error": "Unknown provider."}
    # Anthropic provider with no stored key → borrow the env key for the test.
    if prov.get("type") == "anthropic" and not (prov.get("api_key") or "").strip():
        prov = {**prov, "api_key": os.getenv("ANTHROPIC_API_KEY", "")}
    import time as _t
    t0 = _t.time()
    r = await llm_router.complete(
        prov, "You are a connectivity test. Reply with exactly: OK",
        [{"role": "user", "content": "Say OK"}], max_tokens=16)
    ms = int((_t.time() - t0) * 1000)
    if r.get("error"):
        try:
            import system_llm
            cat = system_llm.classify_error(r["error"])
            label = system_llm.ERROR_LABEL.get(cat, "")
        except Exception:
            cat, label = "error", ""
        return {"ok": False, "error": r["error"], "category": cat, "label": label,
                "ms": ms, "local": llm_router.is_local(prov)}
    out = (r.get("output") or "").strip()
    return {"ok": True, "reply": out[:120], "ms": ms, "local": llm_router.is_local(prov)}


# ── Automatic failover (OFF by default; admin controls the chain) ────────────
@router.get("/llm_failover")
async def get_llm_failover():
    try:
        import system_llm
        fo = system_llm.get_failover()
        return {"ok": True, "enabled": fo["enabled"], "chain": fo["chain"],
                "suggested": system_llm.suggest_chain(exclude_id=system_llm.get_active_id())}
    except Exception as e:
        return {"error": f"Could not read failover: {e}"}


class LLMFailoverRequest(BaseModel):
    enabled: Optional[bool] = None
    chain: Optional[list] = None   # ordered list of provider ids


@router.post("/llm_failover")
async def set_llm_failover(body: LLMFailoverRequest):
    try:
        import system_llm
        fo = system_llm.set_failover(enabled=body.enabled, chain=body.chain)
        return {"ok": True, "enabled": fo["enabled"], "chain": fo["chain"]}
    except Exception as e:
        return {"error": f"Could not save failover: {e}"}


class MCPToolsRequest(BaseModel):
    server_id: str


@router.post("/mcp_tools")
async def mcp_tools(body: MCPToolsRequest):
    s = _get_mcp(body.server_id)
    if not s:
        return {"error": "Unknown MCP server."}
    try:
        import mcp_client
    except Exception:
        return {"error": "MCP client module unavailable on the server."}
    res = await mcp_client.probe(s)
    if res.get("ok"):
        # cache the tool list on the server record
        store = _load(_MCP_PATH, "servers")
        for rec in store.get("servers", []):
            if rec.get("id") == body.server_id:
                rec["tools"] = [{"name": t["name"], "description": t.get("description", "")} for t in res["tools"]]
        _save(_MCP_PATH, store)
    return res


async def workflow_tick(now=None):
    """Scheduler hook: run any workflow whose cron is due (once per minute)."""
    import datetime as _d
    now = now or _d.datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    try:
        from brain.runner import cron_due
    except Exception:
        return
    data = _load(_WF_PATH, "workflows")
    changed = False
    for w in data.get("workflows", []):
        cron = w.get("schedule")
        if not cron or w.get("active", True) is False or not cron_due(cron, now) or w.get("_last_min") == stamp:
            continue
        w["_last_min"] = stamp
        changed = True
        try:
            if w.get("nodes"):
                graph = await _run_graph(w.get("nodes", []), w.get("edges", []), w.get("context", ""))
                run = {"name": w.get("name", "workflow"), "ran_at": graph.get("ran_at"),
                       "step_count": graph.get("step_count", 0),
                       "steps": [{"agent_name": n.get("title", "node"), "answer": n.get("output", ""),
                                  "error": n.get("error")} for n in graph.get("nodes", [])]}
            else:
                run = await _run_steps(w.get("steps", []), w.get("context", ""), w.get("name", "workflow"))
            errored = [s for s in run.get("steps", []) if s.get("error")]
            run["failed"] = bool(errored)
            w["last_run"] = int(time.time())
            _record_run(w["id"], w.get("name"), run)
            if errored:
                await _notify_failure(w, errored)
            # Email the result if recipients are set and SMTP is configured.
            if (w.get("recipients") or "").strip():
                try:
                    import smtp_mailer
                    if smtp_mailer.is_configured():
                        subject, text, html = _run_email_html(run)
                        import asyncio as _a
                        await _a.get_running_loop().run_in_executor(
                            None, lambda: smtp_mailer.send(w["recipients"], subject, text, html))
                except Exception as _e:
                    log.warning(f"workflow email failed: {_e}")
            try:
                import jobs
                jobs.record(f"Workflow: {w['name']}", "workflow", "success", "scheduled")
            except Exception:
                pass
        except Exception as e:
            log.warning(f"scheduled workflow {w.get('id')} failed: {e}")
    if changed:
        _save(_WF_PATH, data)


# NOTE: this catch-all GET must be registered LAST so it does not shadow the
# static GET routes above (/runs, /overview, /templates, /credentials, /mcp_servers).
@router.get("/{wid}")
async def get_workflow(wid: str):
    data = _load(_WF_PATH, "workflows")
    w = next((x for x in data.get("workflows", []) if x["id"] == wid), None)
    return {"ok": True, "workflow": w} if w else {"error": "Not found."}
