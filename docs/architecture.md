# Architecture

## High-level flow

USER
  |
  v
FRONTEND
  |
  | HTTP request
  v
BACKEND API
  |
  | validates and processes input
  v
SPECIALIST / CORE LOGIC
  |
  | ML / AI / algorithm / external model
  v
BACKEND
  |
  | combines results into JSON
  v
FRONTEND
  |
  v
USER SEES RESULT


## Example request flow

1. User enters or uploads data in the frontend.
2. Frontend sends request to backend route.
3. Backend validates the input.
4. Backend calls any preprocessing function.
5. Backend calls specialist/core analysis function.
6. Specialist function returns structured result.
7. Backend combines that result with other data.
8. Backend returns JSON to frontend.
9. Frontend displays result.


## Component responsibilities

Frontend
- User input
- File upload/forms
- Loading states
- Result display
- Error display
- Visualizations

Backend
- API routes
- Request validation
- Authentication if needed
- Database if needed
- Calls specialist functions
- Combines results
- Returns structured JSON

Specialist
- ML models
- AI calls
- algorithms
- embeddings
- ranking
- computer vision
- data processing
- technically novel component

Integration/generalist
- deployment
- testing
- API wiring
- external services
- environment variables
- end-to-end debugging
- demo reliability


## API contract

POST /analyze

INPUT

Depends on project.

Examples:
- text
- image
- audio
- video
- JSON data

OUTPUT

{
  "success": true,
  "result": {},
  "error": null
}

The exact result structure must be agreed on before development.


## Team rule

Every major component must have a defined:

INPUT
OUTPUT
OWNER

before implementation begins.
