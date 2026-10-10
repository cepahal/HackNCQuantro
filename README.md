# HackNCQuantro

Photon setup and the Python `send_summary` adapter are documented in
[backend/services/photon/README.md](backend/services/photon/README.md).
Copy the blank `.env.example` only for a fresh checkout; keep real credentials in ignored `.env`.

Task 1 — Build the iMessage verification agent
Photon
track
 
Person
A

Goal: Get a real message into your backend and return a reply through Photon Spectrum.

Build:

Sign up for Photon and activate the hackathon subscription.

Set up Spectrum according to the 
official Spectrum documentation .

Configure the agent and its messaging number using the supported onboarding flow.

Implement a message handler that recognizes a URL or supported attachment.

Send a confirmation and handle invalid inputs.

Input from Photon: The actual incoming message object provided by Spectrum. Adapt this example to the documented payload rather than assuming these exact fields exist.

json
{
  "messageText": "Check this image: https://example.com/post",
  "senderId": "user-id",
  "attachments": []
}
Output from your handler: A normalized internal request.

json
{
  "requestId": "req_001",
  "inputType": "url",
  "inputUrl": "https://example.com/post",
  "attachmentUrl": null,
  "status": "received"
}
Acceptance test: Text the agent from a real supported iMessage client. It receives the message and replies successfully.

Depends on: Photon account and access to Spectrum.

Parallel work: Person B can begin the Gemini extraction module with test images while Person A configures messaging.

Task 2 — Build media and text extraction
Gemini
track
 
Person
B

Goal: Convert the submitted image or page into useful, structured clues.

Build:

Handle a supported image directly or retrieve an accessible image from a submitted URL.

Extract visible text, captions, dates, locations, watermarks, and visual descriptions where detectable.

Produce search queries that can help locate earlier appearances.

Validate Gemini's response against a schema.

Gemini supports schema-constrained structured output, which is useful for making the result predictable for your next pipeline stages. See the 
Gemini structured output documentation . 

ai.google.dev

Input:

json
{
  "requestId": "req_001",
  "inputType": "url",
  "mediaUrl": "https://example.com/post",
  "mediaType": "image"
}
Output:

json
{
  "requestId": "req_001",
  "extractedText": "A caption visible in the image",
  "visualDescription": "A crowd outside a building",
  "clues": {
    "dates": [],
    "locations": [],
    "watermarks": [],
    "namedEvents": []
  },
  "searchQueries": [
    "\"A caption visible in the image\"",
    "crowd outside building earlier report"
  ],
  "limitations": []
}
Acceptance test: Run five test images and confirm that the extraction output is valid JSON and the text/clues are useful. Empty fields are acceptable when the image does not contain that information.

Depends on: Gemini credentials and a test media set.

Parallel work: Person C can build the report page using mocked extraction and source data.

Task 3 — Build source candidate search
Snowflake
track
 
Person
C

Goal: Find candidate sources and use Snowflake for a real retrieval feature.

I'd make Snowflake Cortex Search your searchable source corpus, not a second copy of your operational database.

Build:

Collect a small, curated corpus of public articles, media references, captions, and source URLs.

Create a Snowflake database, schema, and table containing those records.

Create a Cortex Search Service over a text column containing captions, extracted text, titles, and relevant descriptions.

Query that service through the Snowflake REST API using your search queries.

Normalize the results into your application's candidate-source format.

Snowflake Cortex Search supports hybrid keyword and semantic retrieval over indexed text and exposes a query API. See the 
Cortex Search overview  and 
REST API documentation . 

Snowflake Documentation
+1

Input:

json
{
  "requestId": "req_001",
  "searchQueries": [
    "\"A caption visible in the image\"",
    "crowd outside building earlier report"
  ],
  "limit": 5
}
Output:

json
{
  "requestId": "req_001",
  "candidates": [
    {
      "candidateId": "src_001",
      "url": "https://example.com/earlier-article",
      "title": "Earlier report about an event",
      "publishedAt": "2023-08-14",
      "matchedText": "Matching text from the indexed record",
      "retrievalMethod": "snowflake_cortex_search"
    }
  ]
}
The example result is illustrative. Your application must return actual retrieved records, and publishedAt should be null when the date is unknown.

Acceptance test: Search for a distinctive phrase in your curated corpus and retrieve the correct record and URL.

Important limitation: Snowflake Cortex Search searches the corpus you index; it does not automatically search every social network or discover every original image on the web. Add a separate public-web search adapter if you have access to one. Keep the curated corpus as a dependable demo fallback.

Parallel work: Person D can set up Tiger Data and the operational database schema.

Task 4 — Compare the submitted media with candidate sources
Core
product
 
Person
D

Goal: Establish what evidence supports a source match and identify meaningful differences.

Build:

Compare OCR text and captions.

Compare dates and source context when available.

Use a perceptual image hash or image embeddings for visual similarity if time allows.

Ask Gemini to help interpret the comparison results, but do not use its opinion as the only match signal.

Record each match and difference with its evidence.

Input:

json
{
  "requestId": "req_001",
  "submittedMedia": {
    "extractedText": "A caption visible in the image",
    "visualDescription": "A crowd outside a building"
  },
  "candidates": [
    {
      "candidateId": "src_001",
      "url": "https://example.com/earlier-article",
      "title": "Earlier report about an event",
      "publishedAt": "2023-08-14",
      "matchedText": "Matching text from the indexed record"
    }
  ]
}
Output:

json
{
  "requestId": "req_001",
  "comparisons": [
    {
      "candidateId": "src_001",
      "signals": [
        {
          "type": "text_match",
          "result": "partial",
          "evidence": "A distinctive phrase appears in both records."
        },
        {
          "type": "date",
          "result": "source_predates_submission",
          "evidence": "The recorded source date is earlier."
        }
      ],
      "differences": [],
      "assessment": "possible_match"
    }
  ]
}
Acceptance test: Give the system a known source and a misleadingly captioned repost. It should identify the actual similarities and only report differences that it can support.

Important: A source predating a repost does not prove that it is the original. A similar image does not prove that the caption is false. Preserve that distinction in your data model.

Parallel work: Person A can continue refining Photon follow-ups and the message lifecycle.

Task 5 — Generate the evidence-backed verdict
Gemini
+
Community
 
Person
B

Goal: Turn the extraction and comparison data into a clear, trustworthy explanation.

Build:

Send Gemini the submitted-media clues, candidate sources, and observed comparison signals.

Require a structured report.

Require source references for factual claims.

Explicitly distinguish evidence from interpretation.

Provide a useful “not enough evidence” response when necessary.

Input:

json
{
  "requestId": "req_001",
  "comparisons": [
    {
      "candidateId": "src_001",
      "assessment": "possible_match",
      "signals": [
        {
          "type": "date",
          "result": "source_predates_submission"
        }
      ]
    }
  ],
  "sources": [
    {
      "candidateId": "src_001",
      "url": "https://example.com/earlier-article",
      "title": "Earlier report about an event",
      "publishedAt": "2023-08-14"
    }
  ]
}
Output:

json
{
  "requestId": "req_001",
  "conclusion": "uncertain",
  "summary": "A potentially earlier source was found.",
  "findings": [
    {
      "claim": "The candidate source predates the submitted post.",
      "evidenceSourceIds": ["src_001"],
      "status": "supported"
    }
  ],
  "limitations": [
    "The original source has not been conclusively established."
  ]
}
Acceptance test: For every factual finding, the report contains a traceable evidence source or a documented comparison. If no reliable candidate exists, the system says so instead of making one up.

Community value: This is where you make the tool useful to real people: help them understand context without shaming them for sharing a post.

Task 6 — Build the report website and .tech domain
.Tech
track
 
Person
C

Goal: Turn every investigation into a polished, shareable report.

Build:

A home page with a URL submission box and a sample report.

A report page at /report/[id].

Submitted media and source previews.

Side-by-side comparison.

Source timeline and citations.

Findings, limitations, and a clear explanation of uncertainty.

Deploy to a public host and connect your .tech domain.

Input:

json
{
  "reportId": "req_001",
  "summary": "A potentially earlier source was found.",
  "conclusion": "uncertain",
  "sourceIds": ["src_001"],
  "findings": [
    {
      "claim": "The candidate source predates the submitted post.",
      "evidenceSourceIds": ["src_001"],
      "status": "supported"
    }
  ],
  "limitations": [
    "The original source has not been conclusively established."
  ]
}
Output: A publicly accessible report URL, such as:

https://yourproject.tech/report/req_001

The example domain is a placeholder; check availability before choosing the final name.

Acceptance test: Open the report from a separate browser or device. It displays the correct evidence and source links, and it does not expose API keys or private submissions.

Parallel work: Person D should ensure the database returns everything the report page needs.

Task 7 — Integrate Tiger Data, persistence, and investigation history
Tiger
Data
track
 
Person
D

Goal: Make the entire pipeline persistent and give the agent memory across investigations.

Tiger Data provides a PostgreSQL-compatible cloud database, making it suitable for related investigation, source, and comparison records. See the 
Tiger Data getting-started guide  and 
service quickstart . 

Tiger Data Docs
+1

Build:

Create the Tiger Data service and connect using a PostgreSQL driver.

Create the investigations, source_candidates, and observed_differences tables.

Save the request before processing begins.

Update the status and persist results when processing finishes.

Allow the agent to retrieve an earlier investigation when a user asks about it.

Save basic processing timestamps and error messages for debugging.

Input:

json
{
  "requestId": "req_001",
  "status": "complete",
  "summary": "A potentially earlier source was found.",
  "sourceIds": ["src_001"],
  "conclusion": "uncertain"
}
Output:

json
{
  "reportId": "req_001",
  "persisted": true,
  "status": "complete",
  "reportPath": "/report/req_001"
}
Acceptance test: Complete an investigation, restart the backend, and retrieve the same report. Then send a follow-up request that refers to the earlier report and retrieve the correct stored findings.

How Tiger Data and Snowflake work together:

Tiger Data is the operational source of truth for live investigations, users or conversation identifiers, results, and report IDs.

Snowflake stores the searchable reference corpus and powers source-candidate retrieval.

The backend connects the two by storing Snowflake candidate IDs and source URLs alongside each investigation in Tiger Data.

This makes both sponsor integrations meaningful without unnecessarily duplicating the same workload.

Task 8 — Connect everything, test failures, and prepare the demo
Beginner
+
all
tracks
 
All
four
teammates

Goal: Make the complete user journey work reliably and demonstrate it in under two minutes.

Build:

Connect the Photon message handler to the backend orchestrator.

Connect Gemini extraction, Snowflake retrieval, candidate comparison, and the evidence report.

Persist results in Tiger Data.

Send the final report URL back through Photon.

Test invalid URLs, missing source matches, API errors, duplicate submissions, and inaccessible media.

Prepare three known test examples: a matching image, a caption-altered image, and an example with no reliable source.

Input:

json
{
  "message": "Check this post: https://example.com/test",
  "conversationId": "conversation_001"
}
Output:

json
{
  "status": "complete",
  "reply": "I found a possible earlier source. View the evidence: https://yourproject.tech/report/req_001",
  "reportId": "req_001"
}
Acceptance test: A judge can send a supported input to your agent, receive an evidence-backed answer, and open the full report without manual intervention from your team.

Final demo sequence:

Send a suspicious post to your agent.

Show the investigation starting.

Show the retrieved source and comparison.

Open the report on your .tech domain.

Ask the agent a follow-up question.

Demonstrate how the result remains available in investigation history.

10. How all eight tasks connect
This is the architecture I would actually implement. Each box is a component your teammates can build and test independently.
