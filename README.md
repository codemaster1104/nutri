# Nutri

Nutri is an omnipresent Telegram nutritionist project.
It combines a Telegram bot, FastAPI orchestrator, local Ollama LLM integration, SQLite persistence, and scheduled reminders.

## What it does

- Tracks meals and physical activity.
- Stores user profiles, goals, logs, and reminders.
- Uses Gemma 4 via Ollama for natural language understanding.
- Sends proactive reminders and daily recaps.
- Generates weekly nutrition trend reports.

## Core files

- `main.py`: Orchestrator, Telegram bot handlers, scheduler, and LLM integration.
- `bot_tools.py`: Tool implementations for logging, summaries, profiles, reminders, and queries.
- `database.py`: SQLite schema and session management.
- `data_processor.py`: Weekly nutrition report and trend analysis.
- `.agents/nutri_agent.md`: Local LLM instruction guidance for Gemma 4.
- `.instructions.md`: Project aim and planning summary for assistants.

## Installation

1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd nutri
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Provide your Telegram bot token via an environment variable (recommended):

   PowerShell:
   ```powershell
   $env:TELEGRAM_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
   python main.py
   ```

   Bash / macOS:
   ```bash
   export TELEGRAM_BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
   python main.py
   ```

   Or run using the project virtual environment without exporting:
   ```powershell
   .venv\Scripts\Activate.ps1
   .\.venv\Scripts\python main.py
   ```

## Running Nutri

Start the application:
```bash
python main.py
```

The app runs on `http://0.0.0.0:8000` and the Telegram bot will start polling.

## Telegram commands

- `/start` - Initialize or refresh your profile.
- `/help` - Show help text.
- `/summary` - Get today's nutrition summary.
- `/profile` - View goals and profile.
- `/report` - Generate a 7-day nutrition trend report.
- `/setgoal` - Update nutrition goals.
- `/reminder` - Schedule a reminder.

## Agent docs

- `.agents/nutri_agent.md` — Gemma 4 prompt and tool guidance.
- `.agents/README.md` — explanation of the agent folder.
- `.instructions.md` — project aim and planning summary for assistants.
