"""Юридические страницы для Meta App Review."""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

APP = "ThreadFlow"
CONTACT = "support@threadsmith.pro"
UPDATED = "29 July 2026"

CSS = ("<style>body{font-family:system-ui,sans-serif;max-width:780px;"
       "margin:40px auto;padding:0 20px;line-height:1.65;color:#222}"
       "h1{font-size:27px}h2{font-size:19px;margin-top:30px}"
       "table{border-collapse:collapse;width:100%;margin:14px 0}"
       "td,th{border:1px solid #ddd;padding:8px;text-align:left;font-size:15px}"
       "th{background:#f6f6f6}a{color:#0a58ca}</style>")


def page(title, body):
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>" + title + " - " + APP + "</title>" + CSS +
        "</head><body><h1>" + title + "</h1>" + body +
        "<p style='margin-top:40px;color:#777'>Last updated: " + UPDATED +
        "<br>Contact: " + CONTACT + "</p></body></html>")


@router.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return page("Privacy Policy", """
<p>""" + APP + """ is a Telegram bot that helps a user create, schedule and
publish content to a Threads account that the user owns and connects
themselves. This policy explains what data we process and why.</p>

<h2>Data we collect</h2>
<table>
<tr><th>Data</th><th>Purpose</th></tr>
<tr><td>Telegram user ID, referral source</td><td>Identify your account in the bot</td></tr>
<tr><td>Threads access token (encrypted at rest)</td><td>Act on your behalf via the official Threads API</td></tr>
<tr><td>Threads user id and username</td><td>Link your bot account to your Threads profile</td></tr>
<tr><td>Your writing samples, topics, voice settings</td><td>Generate posts that match your style</td></tr>
<tr><td>Generated and scheduled posts, publication status</td><td>Run the publishing queue and show your history</td></tr>
<tr><td>Post insights (views, likes, replies) for your own posts</td><td>Show performance and improve future generations</td></tr>
<tr><td>Comments under your own posts</td><td>Trigger auto-replies according to rules you configure</td></tr>
<tr><td>Payment records (plan, amount, invoice id)</td><td>Billing. Card data is handled by the payment provider and never reaches our servers</td></tr>
</table>

<h2>Threads permissions we request and why</h2>
<table>
<tr><th>Permission</th><th>How we use it</th></tr>
<tr><td>threads_basic</td><td>Read your Threads profile id and username to link the account</td></tr>
<tr><td>threads_content_publish</td><td>Publish posts you created or approved in the bot</td></tr>
<tr><td>threads_manage_replies</td><td>Read comments under your own posts and post replies you configured</td></tr>
<tr><td>threads_manage_insights</td><td>Read metrics of your own posts to show analytics</td></tr>
<tr><td>threads_keyword_search</td><td>Find public posts on topics in your niche so the bot can suggest relevant content ideas</td></tr>
</table>
<p>We request the minimum scope needed for the features above. We do not use
Threads data for advertising, profiling of other users, or resale.</p>

<h2>Third parties</h2>
<ul>
<li><b>Meta Platforms</b> - Threads API, to publish and read your own content.</li>
<li><b>Anthropic</b> - AI text generation. Prompts contain your topics and
writing samples. They are not used to identify you personally.</li>
<li><b>Supabase</b> - database hosting in the EU (Frankfurt).</li>
<li><b>Telegram</b> - message delivery.</li>
</ul>
<p>We never sell your data and never share it for advertising purposes.</p>

<h2>Storage and retention</h2>
<ul>
<li>Access tokens are stored encrypted and deleted immediately when you
disconnect Threads or when the token expires.</li>
<li>Content and settings are kept while your account is active.</li>
<li>On a deletion request all personal data is erased within 30 days.</li>
<li>Aggregated, non-identifying statistics may be retained.</li>
</ul>

<h2>Compliance</h2>
<p>""" + APP + """ operates in accordance with the Meta Platform Terms and
Developer Policies. Data obtained through the Threads API is used only to
provide the features described above.</p>

<h2>Your rights</h2>
<p>You can request access to, correction of, or deletion of your data at any
time by writing to """ + CONTACT + """. See our
<a href="/data-deletion">data deletion instructions</a>.</p>
""")


@router.get("/terms", response_class=HTMLResponse)
async def terms():
    return page("Terms of Service", """
<p>By using """ + APP + """ you agree to these terms.</p>

<h2>The service</h2>
<p>""" + APP + """ generates, schedules and publishes text content to Threads
accounts that you own and connect yourself. You remain the author and the owner
of all published content and are solely responsible for it.</p>

<h2>Acceptable use</h2>
<ul>
<li>Connect only accounts you own or are authorised to manage.</li>
<li>Do not publish content that violates the Threads Terms of Use, the Meta
Community Guidelines, applicable law, or third-party rights.</li>
<li>Do not use the service for spam, deceptive behaviour, or automated mass
interaction with other users.</li>
<li>Respect the rate limits and automation rules of the Threads platform.</li>
</ul>
<p>We may suspend accounts that break these rules.</p>

<h2>Automation and your responsibility</h2>
<p>The bot can publish and reply automatically according to settings you choose.
You control the level of automation, the frequency, and the rules. You are
responsible for reviewing what is published under your account.</p>

<h2>Credits and plans</h2>
<p>Paid plans grant a monthly credit quota. Credits are consumed per generation.
If a generation fails, credits are returned automatically. Credits are not
transferable and have no cash value.</p>

<h2>Availability</h2>
<p>The service is provided as is. Third-party APIs (Threads, Telegram, AI
providers) may be unavailable or change their rules, which can interrupt
the service.</p>

<h2>Termination</h2>
<p>You may stop using the service and disconnect your Threads account at any
time. Disconnecting removes our access to your Threads account immediately.</p>
""")


@router.get("/data-deletion", response_class=HTMLResponse)
async def data_deletion():
    return page("Data Deletion Instructions", """
<p>You can remove your """ + APP + """ data at any time. There are two levels.</p>

<h2>1. Disconnect your Threads account</h2>
<p>Open the bot and disconnect Threads in the main menu. Your access token is
deleted immediately and """ + APP + """ can no longer read or publish anything
on your behalf. You can also revoke access directly in the Threads app under
Settings, Website permissions, Apps and websites.</p>

<h2>2. Delete all your data</h2>
<p>Send the word DELETE to """ + CONTACT + """ from your email, or write to us
through the bot, including your Telegram username. We erase your data within
30 days and confirm by reply.</p>

<h2>What gets deleted</h2>
<ul>
<li>Telegram identifier and account record</li>
<li>Encrypted Threads access tokens and linked account data</li>
<li>Voice profile, writing samples, topics and autopilot settings</li>
<li>Generated posts, scheduled posts and collected insights</li>
</ul>

<h2>What stays</h2>
<p>Posts already published to Threads remain in your Threads account. You own
them and can delete them there yourself. Billing records are kept where required
by accounting law, without any Threads data attached.</p>
""")
