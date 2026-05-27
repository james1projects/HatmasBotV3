"""
plugins/smite/predictions.py
============================
Twitch predictions: create on match start, resolve on match end.

`_create_prediction` is fired automatically by `_check_live_match`
when a new match is detected (gated behind the 'predictions' feature
toggle). Builds a "Will Hatmaster win this game?" prediction with
Win/Loss outcomes, 120s prediction window.

`resolve_prediction` is called from the dashboard's W/L resolve button
(via webserver action). It:
  1. Records the result in the daily session record
     (`record_result` from _StateMixin, which fires the
     on_match_result callbacks for the economy plugin).
  2. PATCHes the prediction to RESOLVED on Twitch.
  3. Re-applies the stream title so {record} reflects the new W-L.
"""

from __future__ import annotations

from core.config import TWITCH_OWNER_ID


class _PredictionsMixin:
    """
    Mixed into SmitePlugin. Reads/writes:
      self.session             aiohttp session
      self._token_manager      auto-refresh on 401
      self.prediction_id, self._prediction_outcomes
      self.bot                 for send_chat
    Calls _StateMixin (record_result, get_record_string) and
    _TitleMixin (_update_stream_title).
    """

    async def _create_prediction(self):
        """Create a Twitch prediction for the match."""
        try:
            payload = {
                "broadcaster_id": TWITCH_OWNER_ID,
                "title": "Will Hatmaster win this game?",
                "outcomes": [
                    {"title": "Win"},
                    {"title": "Loss"}
                ],
                "prediction_window": 120,
            }
            for attempt in range(2):
                async with self.session.post(
                    "https://api.twitch.tv/helix/predictions",
                    headers=await self._broadcaster_headers(),
                    json=payload
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pred = data["data"][0]
                        self.prediction_id = pred["id"]
                        self._prediction_outcomes = {
                            o["id"]: o["title"] for o in pred.get("outcomes", [])
                        }
                        print(f"[Smite] Prediction created: {self.prediction_id}")
                        # Intentionally no chat announcement — the Twitch
                        # predictions UI already pops the bet drawer for
                        # viewers; an extra chat message just clutters.
                        break
                    elif resp.status == 401 and attempt == 0 and self._token_manager:
                        print("[Smite] Prediction create got 401, refreshing token...")
                        if await self._token_manager.handle_401("broadcaster"):
                            continue
                    body = await resp.text()
                    print(f"[Smite] Prediction creation failed: {resp.status} {body}")
                    break
        except Exception as e:
            print(f"[Smite] Prediction error: {e}")

    async def resolve_prediction(self, outcome):
        """Resolve prediction. outcome = 'win' or 'loss'.
        Also records the result in the daily session record."""
        # Always record the result, even if prediction has expired
        record = self.record_result(outcome)

        if not self.prediction_id:
            print(f"[Smite] No active prediction — recorded {outcome} anyway ({record})")
            # Still update the title with the new record
            god = self.current_god["name"] if self.current_god else None
            await self._update_stream_title(god)
            return

        try:
            # Find the matching outcome ID
            winning_id = None
            target = "Win" if outcome == "win" else "Loss"
            for oid, title in self._prediction_outcomes.items():
                if title == target:
                    winning_id = oid
                    break

            if not winning_id:
                print(f"[Smite] Could not find outcome ID for '{outcome}'")
                return

            payload = {
                "broadcaster_id": TWITCH_OWNER_ID,
                "id": self.prediction_id,
                "status": "RESOLVED",
                "winning_outcome_id": winning_id,
            }
            for attempt in range(2):
                async with self.session.patch(
                    "https://api.twitch.tv/helix/predictions",
                    headers=await self._broadcaster_headers(),
                    json=payload
                ) as resp:
                    if resp.status == 200:
                        result_text = "Hatmaster won!" if outcome == "win" else "Hatmaster lost!"
                        await self.bot.send_chat(
                            f"Prediction resolved! {result_text} (Today: {record})"
                        )
                        print(f"[Smite] Prediction resolved: {outcome} ({record})")
                        self.prediction_id = None
                        self._prediction_outcomes = {}
                        # Update title with new record
                        god = self.current_god["name"] if self.current_god else None
                        await self._update_stream_title(god)
                        break
                    elif resp.status == 401 and attempt == 0 and self._token_manager:
                        print("[Smite] Prediction resolve got 401, refreshing token...")
                        if await self._token_manager.handle_401("broadcaster"):
                            continue
                    body = await resp.text()
                    print(f"[Smite] Resolve failed: {resp.status} {body}")
                    break
        except Exception as e:
            print(f"[Smite] Resolve error: {e}")
