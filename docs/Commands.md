# HatmasBot Commands

Every command starts with `!`. Commands marked **(mods)** need mod or broadcaster status.

## Basic

- **!socials** - Posts YouTube, Bluesky, and Twitch links.
- **!suggest <text>** - Saves a stream suggestion. 60 second cooldown, 500 character limit.
- **!suggestions** (mods) - Shows the total count and the last 3 suggestions.
- **!clearsuggestions** (mods) - Clears every saved suggestion.

## Smite 2

- **!god [name]** - Shows the current god's stats. Add a name to look up any god.
- **!stats** - Shows Ranked Conquest K/D/A, win rate, and KDA.
- **!rank** - Shows current SR and rank tier.
- **!match** - Shows current match status with duration and live KDA.
- **!winrate** - Shows ranked win percentage.
- **!kda** - Shows KDA ratio and KA/D.
- **!damage** - Shows total, per match, and per minute damage.
- **!team** - Lists every teammate with their god and KDA.
- **!lastmatch** - Shows the last completed match result.
- **!record** - Shows today's W-L and win rate.

## Song Requests

- **!sr <song or URL>** - Queues a song. 2 per viewer, 4 per sub, 10 minute max length.
- **!burn <amount>** - Destroy your own Hats on stream (min 500, no refunds). Biggest of the stream and all-time record get called out. **!burns** lists them plus the top burners by total; **!burned** is your own all-time total and rank.
- **!vipsr <song or URL>** - Same as !sr but costs 200 Hats and cuts the line (behind earlier VIP songs). 2 per viewer per hour. No refunds.
- **!skip** (mods) - Skips the current song.
- **!wrongsong** - Removes your most recent queued song.
- **!songlist** - Shows the top 5 queued songs.
- **!song** - Shows the current song, requester, and likes.
- **!like** - Likes the current song. One per viewer per song.
- **!mysongs** - Shows your total likes and most liked song.
- **!toprequester** - Shows the top 3 requesters by likes received.
- **!topsongs** - Shows the top 5 most liked songs.
- **!voteskip** - Votes to skip the current song. Skips at 5 votes.
- **!songstatus** - Shows where your songs sit in the queue plus wait times.
- **!blacklistsong** (mods) - Blacklists the current song or a specific one.

## God Request

- **!godrequest <god>** - Spends 1 God Token to request a god.
- **!godreq <god>** (mods) - Adds a god to the queue for free.
- Add the word **aspect** anywhere in a request (`!godreq Khepri aspect`)
  to request the god's Aspect. Only works for gods that have one
  (auto-checked against the SMITE 2 wiki); an aspect request is a
  separate queue entry from the base god.
- **!godqueue** - Shows the next 5 gods in the queue.
- **!godlist** - Shows the entire queue.
- **!godtokens** - Shows your God Token balance.
- **!godskip** (mods) - Removes the next god from the queue.
- **!remove <pos>** (mods) - Removes the god at a given queue position.
- **!godclear** (mods) - Clears the entire queue.

## Spin Pool

- **!nominate <god>** - Adds a god to the spin pool (1 per viewer per day).
  Add the word **aspect** to nominate the god's Aspect as its own entry.
- **!pool** - Shows the top 5 most-voted pool entries.
- **!spin** (mods) - Picks a weighted-random entry and queues it next.
- **!poolclear** (mods) - Wipes the pool.

## Gamble

- **!gamble <amount, all, half, or quarter>** - Wagers Hats. 10 minimum, 10 second cooldown.
- **!jackpot** - Shows the current jackpot pool.

## Hatmas Market (Economy)

Stock-market-style economy where viewers invest Hats in Smite 2 gods. Prices move based on Hatmaster's match performance.

- **!buy [god] [amount or all]** - Buy shares of a god with hats. Defaults to current god if omitted.
- **!sell [god] [amount or all]** - Sell shares for hats. Defaults to current god if omitted.
- **!portfolio** - Shows your holdings with current value, P&L, and total net worth.
- **!price [god]** - Shows current price, recent trend, and volatility tier.
- **!market** / **!stocks** - Shows the top movers — gainers and losers.
- **!dividend** - Shows the most recent dividend payout info.

## Channel Point Rewards

- **God Joke** (500 points) - Plays a random joke voice line from the current god.
- **God Taunt** (500 points) - Plays a random taunt voice line from the current god.
- **God Laugh** (200 points) - Plays the current god's laugh.
