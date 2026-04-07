"""
Basic Commands Plugin
======================
Simple utility commands for HatmasBot.
Updated for TwitchIO v3.
"""


class BasicPlugin:
    def __init__(self):
        self.bot = None

    def setup(self, bot):
        self.bot = bot
        bot.register_command("hello", self.cmd_hello)
        bot.register_command("commands", self.cmd_commands)
        bot.register_command("uptime", self.cmd_uptime)
        bot.register_command("socials", self.cmd_socials)

    async def cmd_hello(self, message, args, whisper=False):
        name = message.chatter.display_name if message.chatter else "friend"
        await self.bot.send_reply(
            message,
            f"Hello {name}!",
            whisper
        )

    async def cmd_commands(self, message, args, whisper=False):
        cmds = sorted(self.bot._custom_commands.keys())
        if message.chatter and not self.bot.is_mod(message.chatter):
            cmds = [c for c in cmds if not self.bot._custom_commands[c]["mod_only"]]
        await self.bot.send_reply(
            message,
            f"Commands: {', '.join('!' + c for c in cmds)}",
            whisper
        )

    async def cmd_uptime(self, message, args, whisper=False):
        uptime = self.bot.get_uptime()
        await self.bot.send_reply(
            message, f"HatmasBot has been running for {uptime}", whisper
        )

    async def cmd_socials(self, message, args, whisper=False):
        await self.bot.send_reply(
            message,
            "YouTube: youtube.com/@hatmaster | "
            "Bluesky: @hatmasteryt.bsky.social | "
            "Twitch: twitch.tv/hatmaster",
            whisper
        )
