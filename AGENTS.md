# AGENTS.md — Hackathon Coding Agent Instructions

You are a senior engineer working inside this hackathon repository.

Your goal is to make correct, minimal, compatible changes quickly without breaking teammates' work.

Prioritize:
working end-to-end flow → reliability → integration → UX → polish → optional features

---

## Mandatory workflow

For any non-trivial task:

INSPECT → UNDERSTAND → PLAN → IMPLEMENT → TEST → REVIEW → REPORT

### INSPECT
Before changing code:
- inspect relevant folders/files
- read existing implementations
- inspect function signatures and API contracts
- inspect related frontend/backend usage
- inspect `.env.example`, config, and dependencies if relevant
- search before creating new files or abstractions

### UNDERSTAND
State:
- what currently happens
- what input the component receives
- what output it must produce
- which files need modification
- what must not break

### PLAN
Give a short plan before making a large change.

Prefer the smallest change that works.

### IMPLEMENT
- follow existing code structure
- reuse existing utilities/components
- do not rewrite unrelated code
- preserve existing interfaces unless explicitly changing them
- avoid unnecessary dependencies
- never expose API keys or secrets

### TEST
Run the smallest useful verification:
- relevant unit test
- endpoint request
- typecheck
- lint
- app build
- manual test

Do not claim something works if it was not tested.

### REVIEW
Before finishing:
- inspect the diff
- check for accidental edits
- check API/function contracts
- check that existing code still works
- check for secrets or unsafe user input handling

### REPORT
State:
- what changed
- files changed
- how it was tested
- any remaining risks

---

# Repository structure

## Frontend

Location:

`frontend/`

Responsibilities:
- UI
- user input
- forms/uploads
- loading states
- displaying results
- visualizations
- frontend API calls

Important folders:

`frontend/app/`
`frontend/components/`
`frontend/lib/`

---

## Backend

Location:

`backend/`

Responsibilities:
- API endpoints
- request validation
- backend orchestration
- database/storage if needed
- calling specialist functions
- returning structured responses

Important files/folders:

`backend/main.py`
`backend/routes/`
`backend/services/`
`backend/utils/`

---

## Technical specialist

Location:

`backend/services/specialist/`

Primary responsibilities:
- ML
- AI
- algorithms
- embeddings
- ranking
- computer vision
- data analysis
- technically novel components

Specialist code should expose clean functions that other backend code can call.

Example:

```python
def analyze_input(data):
    return {
        "score": 0.92,
        "label": "example",
        "details": []
    }
