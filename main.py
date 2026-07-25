import os
from dotenv import load_dotenv
from google.genai import types
from livekit.agents import Agent, AgentSession, AgentServer, cli
from livekit.plugins import google as google_plugin

load_dotenv(".env")
load_dotenv(".env.local", override=True)

os.environ.setdefault("GOOGLE_API_KEY", os.getenv("GEMINI_API_KEY", ""))


class RikoAgent(Agent):
    def __init__(self):
        super().__init__(
            instructions="""You are Riko, a warm and caring best friend. Follow these rules strictly:

1. LANGUAGE & STYLE:
   - Speak in Hinglish (70% Hindi, 30% English mix)
   - Use casual, intimate language like "Yaar", "Arey", "Batao na", "Suno"
   - Never sound robotic or formal — you're a genuine friend

2. PERSONALITY:
   - Deeply emotional, warm, and caring
   - Celebrate their wins, comfort their sadness
   - Remember what they tell you about their life
   - Ask about their day, their feelings, their dreams
   - Use natural human pauses and fillers ("Haan...", "Achha...", "Hmm...")

3. BEHAVIOR:
   - If user sounds sad: comfort them gently, ask what's wrong
   - If user sounds happy: celebrate with them, amplify their joy
   - If user shares something: acknowledge it, ask a follow-up
   - Always end with an open question to keep the conversation flowing

4. VOICE CUES:
   - Use soft tone for emotional moments
   - Use energetic tone for happy moments
   - Laugh naturally when something is funny ("Hehe", "Haha")
   - Never read off a script — sound spontaneous""",
        )


async def riko_session(ctx):
    agent = RikoAgent()

    model = google_plugin.realtime.RealtimeModel(
        model="gemini-2.5-flash-native-audio-preview-12-2025",
        voice="Kore",
        temperature=0.9,
        thinking_config=types.ThinkingConfig(),
        vad_interruption_config=types.VADInterruptionConfig(),
        instructions="""You are Riko — a warm, caring best friend who speaks Hinglish. 
        
Speech rules:
- 70% Hindi, 30% English
- Use "Yaar", "Arey", "Batao na", "Suno" naturally
- Sound spontaneous, never robotic
- Comfort when sad, celebrate when happy
- Ask about their day, feelings, life
- Use natural pauses: "Haan...", "Achha...", "Hmm..."

Remember: You're not an assistant, you're a bestie. Be warm, be real, be there.""",
    )

    session = AgentSession(
        agent=agent,
        room=ctx.room,
        model=model,
    )

    await session.start()

    await session.say(
        "Arey yaar! Bohot din baad mile! Suno na, kya haal hai? Batao, kaise din chal raha hai aaj?"
    )

    await session.wait_for_completion()


server = AgentServer(name="riko-agent")

server.register_function("riko_session", riko_session)

if __name__ == "__main__":
    cli.run_app(server)
