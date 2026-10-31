# Spike report (2026-10-31)

Metadata from the GitHub/PyPI APIs; installs and runs on Windows 11, Python 3.11.9.

| # | Repo | Spike result | Decision | Reason |
|---|------|--------------|----------|--------|
| 1 | google-gemini/gemini-live-api-examples | Repo is live (pushed 2026-10-23). **No LICENSE file** (GitHub API: no license; /LICENSE is 404). Has `gemini-live-ephemeral-tokens-websocket` and `gemini-live-genai-python-sdk` examples. | **Reference only** | Unlicensed means all rights reserved: we read the protocol, copy no code and write our own client. |
| 2a | google/adk-python (2.13.0, Apache-2.0) | Installs on Windows, imports, builds a CEO with a Developer sub-agent. **Not verified:** real delegation with a model (no API key yet). | **Dropped for now** | Our flow is a fixed pipeline (CEO > Designer > Developer > Tester > fix loop). LLM-driven transfer adds latency and nondeterminism and gives us nothing here. Not removed for being broken, so it can be revisited. |
| 2b | crewAIInc/crewAI (MIT) | **Not spiked.** | Skipped | Same reasoning as ADK, and heavier. |
| 2c | Own loop on google-genai (Apache-2.0) | Chosen. Not yet tested; needs the key. | **Kept** | Smallest, deterministic, and easy to test with a fake model. |
| 3 | microsoft/playwright-python (1.64.1, Apache-2.0) | Chromium installs on Windows; page opened, screenshot taken (4.7 KB PNG), console error captured. | **Kept** | Works as needed. Vision review by Gemini not yet tested. |
| 4 | OpenHands/OpenHands (MIT) | Not evaluated. | Skipped (stretch) | Needs Docker; not a core dependency. |
| 5 | GitHub / Vercel REST via httpx (BSD-3) | httpx installed; the APIs are not yet exercised. | **Kept** | No harness needed. |

Unverified: whether your key has the Live model and ephemeral tokens (v1beta), and whether Gemini TTS works with several voices. These are the first checks once the key is in `backend/.env`.
