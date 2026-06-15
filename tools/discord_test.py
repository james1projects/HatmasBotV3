"""
tools/discord_test.py -- Standalone Discord send tester.

Connects to Discord with the configured bot token WITHOUT starting the
whole bot, then sends a test message to a channel you name (defaults to
DISCORD_DEFAULT_CHANNEL_ID). Use it to confirm the bot can post to a
specific channel -- e.g. a private #bot-test channel you set up.

Usage:
  python tools/discord_test.py                      send default text to the default channel
  python tools/discord_test.py --list               list every channel the bot can see, with ids
  python tools/discord_test.py 123456789012345678   send default text to a specific channel id
  python tools/discord_test.py 1234... "hello there" custom message to a specific channel
  python tools/discord_test.py -m "hi"              custom message to the default channel

The channel argument accepts a raw id, a <#id> mention, or #id.
Exit 0 = message sent (or list shown), 1 = something failed (the line says what).
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import discord
except ImportError:
    print("FAIL  discord.py not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

from core import config

DEFAULT_TEXT = "Test message from HatmasBot (discord_test.py)."


def normalize_channel_id(raw):
    """Accept a raw snowflake, a <#id> mention, or #id. Returns int or None."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s.startswith("<#") and s.endswith(">"):
        s = s[2:-1]
    s = s.lstrip("#")
    if s.isdigit():
        return int(s)
    return None


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="discord_test.py",
        description="Send a test message to a Discord channel (standalone).",
    )
    p.add_argument("channel", nargs="?", default=None,
                   help="Target channel id / <#id> / #id. Default: DISCORD_DEFAULT_CHANNEL_ID")
    p.add_argument("message", nargs="*",
                   help="Message text. Default: a canned test line.")
    p.add_argument("-c", "--channel-id", dest="channel_opt", default=None,
                   help="Alternative way to pass the channel id.")
    p.add_argument("-m", "--message", dest="message_opt", default=None,
                   help="Alternative way to pass the message text.")
    p.add_argument("-l", "--list", action="store_true",
                   help="List every text channel the bot can see (with ids) and exit.")
    p.add_argument("--timeout", type=float, default=20.0,
                   help="Seconds to wait for the gateway before giving up (default 20).")
    return p.parse_args(argv)


def resolve_inputs(args):
    """Return (channel_id|None, message_text). Positional wins over the
    --channel-id / --message flags; falls back to config + canned text."""
    raw_channel = args.channel if args.channel is not None else args.channel_opt
    channel_id = normalize_channel_id(raw_channel)
    if raw_channel is not None and channel_id is None:
        print(f"FAIL  '{raw_channel}' is not a valid channel id (expect a number, "
              f"<#number>, or #number)")
        sys.exit(1)
    if channel_id is None:
        channel_id = int(getattr(config, "DISCORD_DEFAULT_CHANNEL_ID", 0) or 0) or None

    if args.message:
        message = " ".join(args.message)
    elif args.message_opt:
        message = args.message_opt
    else:
        message = DEFAULT_TEXT
    return channel_id, message


async def amain(args):
    token = (getattr(config, "DISCORD_BOT_TOKEN", "") or "").strip()
    if not getattr(config, "DISCORD_ENABLED", False):
        print("WARN  DISCORD_ENABLED is False in config_local.py "
              "(this probe still tries to connect).")
    if not token:
        print("FAIL  No DISCORD_BOT_TOKEN set in core/config_local.py")
        return 1

    channel_id, message = resolve_inputs(args)
    if not args.list and not channel_id:
        print("FAIL  No channel id given and no DISCORD_DEFAULT_CHANNEL_ID configured.\n"
              "      Pass one: python tools/discord_test.py <channel_id> \"your message\"\n"
              "      Or find ids with: python tools/discord_test.py --list")
        return 1

    intents = discord.Intents.default()  # 'guilds' is enough to see channels and send
    client = discord.Client(intents=intents)
    state = {"code": 1}

    @client.event
    async def on_ready():
        try:
            who = client.user
            guilds = client.guilds
            print(f"OK    connected as {who} -> {len(guilds)} guild(s): "
                  f"{', '.join(g.name for g in guilds) or 'none'}")

            if args.list:
                _list_channels(client)
                state["code"] = 0
                return

            channel = client.get_channel(channel_id)
            if channel is None:
                # Not in the cache -- ask the API directly. This is the
                # real test of 'can the bot reach THIS channel'.
                try:
                    channel = await client.fetch_channel(channel_id)
                except discord.NotFound:
                    print(f"FAIL  channel {channel_id} not found "
                          f"(wrong id, or the bot isn't in that server).")
                    return
                except discord.Forbidden:
                    print(f"FAIL  no access to channel {channel_id} "
                          f"(invite the bot / grant it View Channel).")
                    return

            if not hasattr(channel, "send"):
                print(f"FAIL  channel {channel_id} ({type(channel).__name__}) "
                      f"is not a text channel you can post in.")
                return

            ch_name = getattr(channel, "name", str(channel_id))
            g_name = getattr(getattr(channel, "guild", None), "name", "DM")
            try:
                sent = await channel.send(message)
            except discord.Forbidden:
                print(f"FAIL  forbidden -- the bot lacks Send Messages in "
                      f"#{ch_name} ({g_name}). Check the channel's role permissions.")
                return
            print(f"OK    sent to #{ch_name} ({g_name}) | message id {sent.id}")
            print(f"      content: {message}")
            state["code"] = 0
        except Exception as e:
            print(f"FAIL  unexpected error: {type(e).__name__}: {e}")
        finally:
            await client.close()

    try:
        await asyncio.wait_for(client.start(token), timeout=args.timeout)
    except asyncio.TimeoutError:
        print(f"FAIL  timed out after {args.timeout:.0f}s waiting for the gateway.")
        if not client.is_closed():
            await client.close()
    except discord.LoginFailure:
        print("FAIL  login failed -- bad or expired DISCORD_BOT_TOKEN. "
              "Regenerate it in the dev portal.")
    except discord.PrivilegedIntentsRequired as e:
        print(f"FAIL  privileged intents required: {e}")
    return state["code"]


def _list_channels(client):
    print("      Visible text channels (server / #channel / id):")
    any_ch = False
    for guild in client.guilds:
        for ch in guild.channels:
            if hasattr(ch, "send"):  # text-like, postable
                any_ch = True
                print(f"        {guild.name} / #{ch.name} / {ch.id}")
    if not any_ch:
        print("        (none -- is the bot invited with View Channels?)")


def main():
    args = parse_args(sys.argv[1:])
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
