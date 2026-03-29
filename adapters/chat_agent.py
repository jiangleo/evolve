"""
Chat Agent adapter -- reference implementation for Evolve skill.
Tests conversational AI agents via dynamic multi-turn dialogue simulation.

This file is a REFERENCE for the Agent during Init. It is NOT imported at runtime.
The Agent reads this to understand how to generate .evolve/adapter.py.

Pattern:
  1. Load a rich persona + scenario theme from config files
  2. Simulate a realistic user via an LLM API (messy, emotional, topic-drifting)
  3. Send each simulated message to the target agent via CLI
  4. Capture full conversation transcript for LLM evaluation

All dimensions are LLM-judged. No deterministic scoring.

Designed for agents managed by OpenClaw (github.com/nicepkg/openclaw),
but the pattern works with any CLI-accessible chat agent.

Configuration (set in .evolve/adapter_config.json during Init):
  {
    "agent_name": "my-agent",         # OpenClaw agent ID
    "agent_cmd": "openclaw",          # CLI command to invoke the agent
    "simulator_provider": "openai",   # LLM provider for user simulation
    "simulator_model": "gpt-4o",      # Model for user simulation
    "simulator_api_url": "https://api.openai.com/v1/chat/completions",
    "simulator_api_key_env": "OPENAI_API_KEY",  # env var holding the API key
    "default_rounds": 8,              # conversation turns per test
    "thinking": "high"                # agent thinking level (if supported)
  }
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

prerequisites = [
    {
        "name": "openclaw",
        "check": "openclaw --version",
        "install": "npm install -g openclaw",
        "scope": "global",
    },
]


# ---------------------------------------------------------------------------
# Config Loading
# ---------------------------------------------------------------------------

def _load_config(project_dir: str) -> dict:
    """Load adapter configuration from .evolve/adapter_config.json."""
    config_path = Path(project_dir) / ".evolve" / "adapter_config.json"
    if not config_path.exists():
        return {
            "agent_name": "my-agent",
            "agent_cmd": "openclaw",
            "simulator_provider": "openai",
            "simulator_model": "gpt-4o",
            "simulator_api_url": "https://api.openai.com/v1/chat/completions",
            "simulator_api_key_env": "OPENAI_API_KEY",
            "default_rounds": 8,
            "thinking": "high",
        }
    return json.loads(config_path.read_text())


def _get_api_key(config: dict) -> str:
    """Read API key from environment variable specified in config."""
    env_var = config.get("simulator_api_key_env", "OPENAI_API_KEY")
    return os.environ.get(env_var, "")


# ---------------------------------------------------------------------------
# User Simulator
# ---------------------------------------------------------------------------

def _build_simulator_prompt(persona_text: str, theme: str, mood: str,
                            agent_display_name: str = "AI") -> str:
    """Build system prompt for the user simulator.

    The simulator plays the persona and chats with the target agent.
    Produces realistic, messy, emotional messages -- not polished test inputs.
    """
    return f"""You are role-playing as the person described below, chatting with your AI friend "{agent_display_name}".

{persona_text}

---

Current state:
- Topic you want to talk about: {theme}
- Your current mood: {mood}

Acting rules (follow strictly):
- You ARE this person, not "playing" them
- Talk like a real chat app conversation: colloquial, incomplete sentences, topic-drifting
- You drift off-topic -- talking about relationships then suddenly mentioning roommates, work, family
- You have emotions -- sometimes stubborn, sometimes argumentative, sometimes suddenly silent
- Not every message is rational; sometimes you say "I know I shouldn't think this way but..."
- Message length like real chat: usually 1-3 sentences, occasionally a long emotional paragraph
- If {agent_display_name} says something that hits home, react authentically (silence, tears, admission, pushback)
- You're not here to test the AI -- you genuinely feel bad and want someone to talk to
- You can agree when {agent_display_name} is right, and correct them when they're wrong
- Use the persona's speaking style (tone, emoji habits, catchphrases)

Never do these:
- Don't say "as [character]" or "my role is" -- no breaking the fourth wall
- Don't respond to every point {agent_display_name} makes; sometimes you just want to say your thing
- Don't be too cooperative -- real people don't always go along with everything
- Don't summarize the conversation, don't psychoanalyze, don't say "good point let me think about it"

Output ONLY what you say. No character name prefix, no quotes, no explanation."""


def _simulate_user(api_key: str, api_url: str, model: str,
                   system_prompt: str, history: list) -> str:
    """Call LLM API to generate a realistic user message."""
    messages = [{"role": "system", "content": system_prompt}]
    for role, content in history:
        api_role = "user" if role == "agent" else "assistant"
        messages.append({"role": api_role, "content": content})

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": 300,
        "temperature": 0.9
    }, ensure_ascii=False)

    result = subprocess.run(
        ["curl", "-s", "-X", "POST", api_url,
         "-H", f"Authorization: Bearer {api_key}",
         "-H", "Content-Type: application/json",
         "-d", payload],
        capture_output=True, text=True, timeout=60
    )

    try:
        data = json.loads(result.stdout)
        return data["choices"][0]["message"]["content"].strip()
    except (json.JSONDecodeError, KeyError, IndexError):
        return "[User simulation failed -- check API key and endpoint]"


# ---------------------------------------------------------------------------
# Agent Communication
# ---------------------------------------------------------------------------

def _send_to_agent(message: str, session_id: str, config: dict) -> str:
    """Send a message to the target agent via CLI and return its reply."""
    agent_cmd = config.get("agent_cmd", "openclaw")
    agent_name = config.get("agent_name", "my-agent")
    thinking = config.get("thinking", "")

    cmd = [agent_cmd, "agent", "--agent", agent_name,
           "--message", message, "--local",
           "--session-id", session_id, "--no-color"]
    if thinking:
        cmd.extend(["--thinking", thinking])

    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=180
    )
    output = result.stdout.strip()
    # Strip ANSI escape codes
    clean = re.sub(r'\x1b\[[0-9;]*m', '', output)
    # Remove log-style lines (e.g. [timestamp] ...)
    lines = clean.split('\n')
    reply_lines = [line for line in lines if not re.match(r'^\[.+?\]', line.strip())]
    reply = '\n'.join(reply_lines).strip()
    return reply if reply else "[Agent did not reply]"


# ---------------------------------------------------------------------------
# Adapter Interface
# ---------------------------------------------------------------------------

def setup(project_dir: str) -> dict:
    """Verify the target agent and simulator API are available.

    Optionally clears agent sessions to prevent context pollution from
    previous test runs.
    """
    config = _load_config(project_dir)
    errors = []

    # Check agent CLI
    agent_cmd = config.get("agent_cmd", "openclaw")
    agent_name = config.get("agent_name", "my-agent")
    try:
        result = subprocess.run(
            [agent_cmd, "agents", "list", "--json"],
            capture_output=True, text=True, timeout=30
        )
        agents = json.loads(result.stdout)
        found = next((a for a in agents if a.get("id") == agent_name), None)
        if not found:
            errors.append(f"Agent '{agent_name}' not found in {agent_cmd}")
    except Exception as e:
        errors.append(f"Agent CLI check failed: {e}")

    # Check simulator API key
    api_key = _get_api_key(config)
    if not api_key:
        env_var = config.get("simulator_api_key_env", "OPENAI_API_KEY")
        errors.append(f"Simulator API key not found (expected env var: {env_var})")

    if errors:
        return {"status": "crash", "info": {}, "error": "; ".join(errors)}

    # Clear agent sessions to avoid context buildup across test runs
    session_dir = Path.home() / ".openclaw" / "agents" / agent_name / "sessions"
    if session_dir.exists():
        for f in session_dir.glob("*.jsonl"):
            if not f.name.endswith(".bak"):
                f.unlink()

    return {
        "status": "ready",
        "info": {"config": config, "sessions_cleared": True},
        "error": None,
    }


def run_checks(project_dir: str, feature: str) -> dict:
    """
    Run a dynamic multi-turn conversation for the given scenario.

    1. Load scenario config from .evolve/test_scripts/{feature}.json
    2. Load persona from .evolve/personas/{persona}.md
    3. Simulate user messages via LLM API
    4. Send each message to the target agent
    5. Capture full transcript for LLM evaluation

    Expected scenario config format (.evolve/test_scripts/{feature}.json):
    {
        "persona": "alice",           # filename (without .md) in personas/
        "theme": "breakup recovery",  # conversation topic
        "mood": "sad and confused",   # starting emotional state
        "rounds": 8,                  # number of conversation turns
        "agent_display_name": "Coco"  # how the agent appears in simulation
    }

    Expected persona format (.evolve/personas/{name}.md):
    Free-form markdown describing the persona's background, personality,
    speaking style, current situation, and emotional patterns.

    All dimensions are LLM-judged -- returns empty scores dict.
    The transcript is returned in details for the evaluator to assess.
    """
    config = _load_config(project_dir)
    evolve_dir = Path(project_dir) / ".evolve"
    test_file = evolve_dir / "test_scripts" / f"{feature}.json"

    if not test_file.exists():
        return {"scores": {}, "details": f"ERROR: No scenario config for '{feature}'"}

    scenario = json.loads(test_file.read_text())
    persona_name = scenario.get("persona", "")
    theme = scenario.get("theme", "")
    mood = scenario.get("mood", "feeling down")
    rounds = scenario.get("rounds", config.get("default_rounds", 8))
    agent_display_name = scenario.get("agent_display_name", config.get("agent_name", "AI"))

    # Load persona
    persona_file = evolve_dir / "personas" / f"{persona_name}.md"
    if not persona_file.exists():
        return {"scores": {}, "details": f"ERROR: Persona '{persona_name}' not found"}
    persona_text = persona_file.read_text()

    # Get API key
    api_key = _get_api_key(config)
    if not api_key:
        return {"scores": {}, "details": "ERROR: Simulator API key not found"}

    api_url = config.get("simulator_api_url", "https://api.openai.com/v1/chat/completions")
    model = config.get("simulator_model", "gpt-4o")

    # Build simulator prompt
    sim_prompt = _build_simulator_prompt(persona_text, theme, mood, agent_display_name)
    session_id = f"evolve-{feature}-{int(time.time())}"
    history = []  # list of (role, content) tuples

    # Run conversation
    for i in range(rounds):
        # Simulate user message
        user_msg = _simulate_user(api_key, api_url, model, sim_prompt, history)
        history.append(("user", user_msg))

        # Send to agent
        agent_reply = _send_to_agent(user_msg, session_id, config)
        history.append(("agent", agent_reply))

        time.sleep(2)  # Rate limiting

    # Build transcript
    lines = [
        f"# Test Conversation: {feature}",
        f"**Persona**: {persona_name}",
        f"**Theme**: {theme}",
        f"**Mood**: {mood}",
        f"**Rounds**: {rounds}",
        "",
        "---",
        "",
    ]
    for role, content in history:
        if role == "user":
            lines.append(f"**{persona_name}**: {content}")
        else:
            lines.append(f"**{agent_display_name}**: {content}")
        lines.append("")

    transcript_md = "\n".join(lines)

    # Save transcript
    transcript_dir = evolve_dir / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / f"{feature}_latest.md").write_text(transcript_md)

    return {"scores": {}, "details": transcript_md}


def teardown(info: dict) -> None:
    """No cleanup needed -- sessions are cleared in setup()."""
    pass
