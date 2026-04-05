"""
Claude Chat Plugin
===================
Handles @HatmasBot mentions with Claude API responses.
Rate limited per user and globally.
"""

import time
import anthropic

from core.config import (
    CLAUDE_API_KEY, CLAUDE_MODEL, CLAUDE_MAX_TOKENS,
    CLAUDE_COOLDOWN_USER, CLAUDE_COOLDOWN_GLOBAL,
    CLAUDE_SYSTEM_PROMPT, TWITCH_BOT_USERNAME
)


class ClaudeChatPlugin:
    def __init__(self):
        self.bot = None
        self.client = None
        self.last_use_global = 0
        self.last_use_per_user = {}  # username -> timestamp

    def setup(self, bot):
        self.bot = bot
        bot.register_mention_handler(self.handle_mention)

    async def on_ready(self):
        try:
            self.client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
            print("[Claude] API client initialized")
        except Exception as e:
            print(f"[Claude] Failed to initialize: {e}")

    async def handle_mention(self, message):
        if not self.bot.is_feature_enabled("claude_chat"):
            return

        if not self.client:
            return

        username = message.chatter.name.lower()
        now = time.time()

        # Global cooldown
        if now - self.last_use_global < CLAUDE_COOLDOWN_GLOBAL:
            return

        # Per-user cooldown
        last_user = self.last_use_per_user.get(username, 0)
        if now - last_user < CLAUDE_COOLDOWN_USER:
            remaining = int(CLAUDE_COOLDOWN_USER - (now - last_user))
            await self.bot.send_chat(
                f"@{message.chatter.name} Cooldown: {remaining}s"
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

        try:
            response = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                system=CLAUDE_SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"[{message.chatter.name} in Twitch chat says]: {content}"
                }]
            )

            reply = response.content[0].text

            # Truncate for Twitch's 500 char limit
            if len(reply) > 450:
                reply = reply[:447] + "..."

            await self.bot.send_chat(f"@{message.chatter.name} {reply}")

        except Exception as e:
            print(f"[Claude] API error: {e}")
