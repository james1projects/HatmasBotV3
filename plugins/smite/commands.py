"""
plugins/smite/commands.py
=========================
Chat command handlers for the smite plugin.

Surface (matches HatmasBot.md):
  !god [name]    — Current god live stats, or look up any god's profile aggregates
  !stats         — Ranked Conquest K/D/A, win rate, KDA
  !rank          — Current SR + tier (live if in match, else profile)
  !match         — In-match status with duration + live KDA
  !winrate       — Ranked win percentage
  !kda           — KDA ratio (live if in match, else lifetime)
  !damage        — Total / per-match / per-min damage
  !team          — All players on Hatmaster's team this match
  !lastmatch     — Last completed match results
  !record        — Today's W-L + win rate

All commands lean on _MatchStateMixin for live state + tracker.gg
helpers, _TrackerClientMixin for profile fetches, and _StateMixin for
the daily record.
"""

from __future__ import annotations

import time


class _CommandsMixin:
    """
    Mixed into SmitePlugin. Reads:
      self.bot, self.current_god, self.is_in_match, self.match_start_time,
      self.match_players, self.last_match_result,
      self._session_wins, self._session_losses
    Calls _TrackerClientMixin (_fetch_profile), _MatchStateMixin
    (_get_gamemode_stats, _get_god_stats, _stat_display, _stat_val),
    _StateMixin (get_record_string).
    """

    async def cmd_god(self, message, args, whisper=False):
        """!god — Show current god (live) or look up god stats."""
        if not args and self.current_god:
            # Live god info
            g = self.current_god
            s = g["stats"]
            await self.bot.send_reply(
                message,
                f"Currently playing {g['name']} ({g['team'].title()} side) | "
                f"KDA: {s['kills']}/{s['deaths']}/{s['assists']} | "
                f"Gold: {s['gold']:,} ({s['gpm']} GPM)",
                whisper
            )
            return

        if not args and not self.current_god:
            await self.bot.send_reply(
                message,
                "Not in a match right now. Use !god <name> to look up a god.",
                whisper
            )
            return

        # Look up specific god stats from profile
        god_name = args.strip()
        data = await self._fetch_profile()
        if not data:
            await self.bot.send_reply(message, "Couldn't fetch stats right now.", whisper)
            return

        god = self._get_god_stats(data, god_name)
        if not god:
            await self.bot.send_reply(
                message, f"No stats found for {god_name}.", whisper
            )
            return

        stats = god.get("stats", {})
        name = god.get("metadata", {}).get("name", god_name)
        kills = self._stat_display(stats, "kills")
        deaths = self._stat_display(stats, "deaths")
        assists = self._stat_display(stats, "assists")
        matches = self._stat_display(stats, "matchesPlayed")
        winrate = self._stat_display(stats, "matchesWinPct")

        await self.bot.send_reply(
            message,
            f"{name}: {matches} games | {winrate} WR | K/D/A: {kills}/{deaths}/{assists}",
            whisper
        )

    async def cmd_stats(self, message, args, whisper=False):
        """!stats — Ranked conquest overview."""
        data = await self._fetch_profile()
        if not data:
            await self.bot.send_reply(message, "Couldn't fetch stats right now.", whisper)
            return

        stats = self._get_gamemode_stats(data)
        if not stats:
            await self.bot.send_reply(message, "No ranked conquest stats found.", whisper)
            return

        kills = self._stat_display(stats, "kills")
        deaths = self._stat_display(stats, "deaths")
        assists = self._stat_display(stats, "assists")
        winrate = self._stat_display(stats, "matchesWinPct")
        matches = self._stat_display(stats, "matchesPlayed")
        kda = self._stat_display(stats, "kdaRatio")

        await self.bot.send_reply(
            message,
            f"Ranked Conquest: {matches} games | {winrate} WR | "
            f"K/D/A: {kills}/{deaths}/{assists} | KDA: {kda}",
            whisper
        )

    async def cmd_rank(self, message, args, whisper=False):
        """!rank — Current SR and rank tier."""
        # Try live match first (most accurate SR)
        if self.current_god and self.current_god.get("rank"):
            r = self.current_god["rank"]
            await self.bot.send_reply(
                message, f"Rank: {r['tier']} (SR: {r['sr']})", whisper
            )
            return

        # Fall back to profile
        data = await self._fetch_profile()
        stats = self._get_gamemode_stats(data) if data else None
        if not stats:
            await self.bot.send_reply(message, "Couldn't fetch rank.", whisper)
            return

        matches = self._stat_display(stats, "matchesPlayed")
        wins = self._stat_display(stats, "matchesWon")
        losses = self._stat_display(stats, "matchesLost")

        await self.bot.send_reply(
            message, f"Ranked: {matches} games | {wins}W {losses}L", whisper
        )

    async def cmd_match(self, message, args, whisper=False):
        """!match — Check if currently in a match."""
        if self.is_in_match:
            if self.current_god:
                g = self.current_god
                s = g["stats"]
                duration = int(time.time() - self.match_start_time) if self.match_start_time else 0
                mins = duration // 60
                await self.bot.send_reply(
                    message,
                    f"In a match ({mins} min) playing {g['name']} | "
                    f"KDA: {s['kills']}/{s['deaths']}/{s['assists']}",
                    whisper
                )
            else:
                await self.bot.send_reply(
                    message, "In a match, still detecting god...", whisper
                )
        else:
            await self.bot.send_reply(message, "No active match right now.", whisper)

    async def cmd_winrate(self, message, args, whisper=False):
        """!winrate — Ranked win rate."""
        data = await self._fetch_profile()
        stats = self._get_gamemode_stats(data) if data else None
        if not stats:
            await self.bot.send_reply(message, "Couldn't fetch stats.", whisper)
            return
        wr = self._stat_display(stats, "matchesWinPct")
        await self.bot.send_reply(message, f"Win rate: {wr}", whisper)

    async def cmd_kda(self, message, args, whisper=False):
        """!kda — KDA ratio."""
        # Live KDA if in match
        if self.current_god:
            s = self.current_god["stats"]
            await self.bot.send_reply(
                message,
                f"Live KDA ({self.current_god['name']}): "
                f"{s['kills']}/{s['deaths']}/{s['assists']}",
                whisper
            )
            return

        data = await self._fetch_profile()
        stats = self._get_gamemode_stats(data) if data else None
        if not stats:
            await self.bot.send_reply(message, "Couldn't fetch stats.", whisper)
            return
        kda = self._stat_display(stats, "kdaRatio")
        kad = self._stat_display(stats, "kadRatio")
        await self.bot.send_reply(message, f"KDA: {kda} | KA/D: {kad}", whisper)

    async def cmd_damage(self, message, args, whisper=False):
        """!damage — Damage stats."""
        data = await self._fetch_profile()
        stats = self._get_gamemode_stats(data) if data else None
        if not stats:
            await self.bot.send_reply(message, "Couldn't fetch stats.", whisper)
            return
        total = self._stat_display(stats, "damage")
        per_match = self._stat_display(stats, "damagePerMatch")
        per_min = self._stat_display(stats, "damagePerMinute")
        matches = self._stat_display(stats, "matchesPlayed")
        await self.bot.send_reply(
            message,
            f"Total damage: {total} across {matches} games | "
            f"{per_match}/game | {per_min}/min",
            whisper
        )

    async def cmd_team(self, message, args, whisper=False):
        """!team — Show all players on your team in the current match."""
        if not self.is_in_match or not self.match_players:
            await self.bot.send_reply(message, "No match data available.", whisper)
            return

        my_team = "unknown"
        for p in self.match_players:
            if p.get("is_me"):
                my_team = p.get("team", "unknown")
                break

        teammates = [p for p in self.match_players if p["team"] == my_team]
        team_str = " | ".join(
            f"{p['god']} ({p['kills']}/{p['deaths']}/{p['assists']})"
            for p in teammates
        )
        await self.bot.send_reply(
            message, f"{my_team.title()} team: {team_str}", whisper
        )

    async def cmd_lastmatch(self, message, args, whisper=False):
        """!lastmatch — Show results from the last completed match."""
        if not self.last_match_result:
            await self.bot.send_reply(message, "No recent match data.", whisper)
            return

        r = self.last_match_result
        s = r.get("stats", {})
        await self.bot.send_reply(
            message,
            f"Last game: {r['god']} "
            f"{s.get('kills', 0)}/{s.get('deaths', 0)}/{s.get('assists', 0)}",
            whisper
        )

    async def cmd_record(self, message, args, whisper=False):
        """!record — Show today's win/loss record."""
        record = self.get_record_string()
        total = self._session_wins + self._session_losses
        if total == 0:
            await self.bot.send_reply(message, "No games played today yet!", whisper)
        else:
            pct = round(self._session_wins / total * 100) if total > 0 else 0
            await self.bot.send_reply(
                message,
                f"Today's record: {record} ({pct}% WR across {total} games)",
                whisper
            )
