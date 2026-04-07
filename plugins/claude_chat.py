"""
Claude Chat Plugin
===================
Handles @HatmasBot mentions with Claude API responses.
Rate limited per user and globally.
Per-user conversation history is persisted to disk and the last 10
messages are sent as context with each API call.
"""

import json
import time
import anthropic

from core.config import (
    CLAUDE_API_KEY, CLAUDE_MODEL, CLAUDE_MAX_TOKENS,
    CLAUDE_COOLDOWN_USER, CLAUDE_COOLDOWN_GLOBAL,
    CLAUDE_SYSTEM_PROMPT, TWITCH_BOT_USERNAME, DATA_DIR
)

CLAUDE_HISTORY_FILE = DATA_DIR / "claude_history.json"
CLAUDE_CONTEXT_LIMIT = 10  # Max messages (user+assistant pairs) sent to API


class ClaudeChatPlugin:
    def __init__(self):
        self.bot = None
        self.client = None
        self.last_use_global = 0
        self.last_use_per_user = {}  # username -> timestamp
        self.history = {}  # username -> [{"role": ..., "content": ...}, ...]
        self._load_history()

    def _load_history(self):
        """Load full conversation history from disk."""
        try:
            if CLAUDE_HISTORY_FILE.exists():
                with open(CLAUDE_HISTORY_FILE, "r") as f:
                    self.history = json.load(f)
                print(f"[Claude] Loaded history for {len(self.history)} users")
        except Exception as e:
            print(f"[Claude] Failed to load history: {e}")
            self.history = {}

    def _save_history(self):
        """Persist full conversation history to disk."""
        try:
            with open(CLAUDE_HISTORY_FILE, "w") as f:
                json.dump(self.history, f, indent=2)
        except Exception as e:
            print(f"[Claude] Failed to save history: {e}")

    def _get_context(self, username):
        """Return the last CLAUDE_CONTEXT_LIMIT messages for API context."""
        msgs = self.history.get(username, [])
        return msgs[-(CLAUDE_CONTEXT_LIMIT * 2):]  # pairs of user+assistant

    def _append_history(self, username, role, content):
        """Append a message to a user's history and save."""
        if username not in self.history:
            self.history[username] = []
        self.history[username].append({"role": role, "content": content})
        self._save_history()

    @staticmethod
    def _sanitize_response(text):
        """Strip dangerous leading characters and block command-like output.

        Prevents prompt-injection attacks where a chatter tricks the model
        into outputting a Twitch command (! or /) or a bot dot-command (.).
        """
        # Strip leading whitespace first
        text = text.strip()

        # Strip any leading command prefixes the model was tricked into
        while text and text[0] in ("!", "/", "."):
            text = text[1:].lstrip()

        return text

    def setup(self, bot):
        self.bot = bot
        bot.register_mention_handler(self.handle_mention)

    async def on_ready(self):
        try:
            self.client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
            print("[Claude] API client initialized")
        except Exception as e:
            print(f"[Claude] Failed to initialize: {e}")

    async def handle_mention(self, message, whisper=False):
        if not self.bot.is_feature_enabled("claude_chat"):
            return

        if not self.client:
            return

        # Whispered @HatmasBot messages — politely redirect to chat
        if whisper:
            await self.bot.send_reply(
                message,
                "I can only reply in the main chat, not through whispers. "
                "Try @ing me in chat instead!",
                whisper=True,
            )
            return

        username = message.chatter.name.lower()
        display_name = message.chatter.display_name or message.chatter.name
        now = time.time()

        # Global cooldown
        if now - self.last_use_global < CLAUDE_COOLDOWN_GLOBAL:
            return

        # Per-user cooldown
        last_user = self.last_use_per_user.get(username, 0)
        if now - last_user < CLAUDE_COOLDOWN_USER:
            remaining = int(CLAUDE_COOLDOWN_USER - (now - last_user))
            await self.bot.send_chat(
                f"@{display_name} Cooldown: {remaining}s"
            )
            return

        # Extract the message (remove the @HatmasBot mention)
        content = message.text
        for mention in [f"@{TWITCH_BOT_USERNAME}", TWITCH_BOT_USERNAME.lower(),
                        f"@{TWITCH_BOT_USERNAME.lower()}"]:
            content = content.replace(mention, "").strip()

        if not content:
            return

        # Update cooldowns
        self.last_use_global = now
        self.last_use_per_user[username] = now

        # Build messages with conversation context
        user_msg = f"[{display_name} in Twitch chat says]: {content}"
        self._append_history(username, "user", user_msg)
        api_messages = self._get_context(username)

        try:
            response = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                system=CLAUDE_SYSTEM_PROMPT,
                messages=api_messages,
            )

            reply = response.content[0].text

            # Sanitize: strip leading command characters that chatters
            # might trick the model into producing (! / .)
            reply = self._sanitize_response(reply)

            if not reply:
                return

            # Save assistant reply to history
            self._append_history(username, "assistant", reply)

            # Truncate for Twitch's 500 char limit
            if len(reply) > 450:
                reply = reply[:447] + "..."

            await self.bot.send_chat(f"@{display_name} {reply}")

        except Exception as e:
            print(f"[Claude] API error: {e}")

    async def cleanup(self):
        self._save_history()
