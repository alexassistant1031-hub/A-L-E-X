# Mark 2 — Riko Voice AI Assistant

A voice AI assistant built with **LiveKit Agents** and **Google Gemini Multimodal Live API**.
Audio-to-audio conversation with built-in VAD and interruption support. Speaks in warm,
caring **Hinglish** (Hindi-English mix) — feels like a genuine friend.

## Prerequisites

- Python >= 3.10
- A [LiveKit Cloud](https://cloud.livekit.io) account
- A [Google AI Studio](https://aistudio.google.com) API key

## Setup

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure API keys
cp .env .env.local
# Edit .env.local with your actual API keys
```

## API Keys Required

| Variable           | Source                  |
|--------------------|-------------------------|
| `GEMINI_API_KEY`   | Google AI Studio        |
| `LIVEKIT_URL`      | LiveKit Cloud dashboard |
| `LIVEKIT_API_KEY`  | LiveKit Cloud dashboard |
| `LIVEKIT_API_SECRET` | LiveKit Cloud dashboard |

## Usage

```bash
# Console mode (CLI test)
python main.py console

# Dev mode (LiveKit playground)
python main.py dev
```

### Testing

1. Run the agent in dev mode
2. Open LiveKit Cloud playground in your browser
3. Connect to the same room
4. Start talking — Riko will respond in Hinglish

## Model

Currently using `gemini-2.5-flash-native-audio-preview-12-2025` with "Kore" voice.
Update `main.py` when newer models are available.
