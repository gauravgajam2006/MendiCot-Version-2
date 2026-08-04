# Secure shuffle and deal contract

## Versioned algorithms

- `deck_definition_version`: `mendicot-48-v1`
- `shuffle_algorithm_version`: `hmac-sha256-fisher-yates-v1`
- `dealing_algorithm_version`: `round-robin-seat-order-v1`
- `context_version`: `mendicot-shuffle-context-v1`

The canonical deck has 48 cards. Its suit order is `SPADES`, `HEARTS`, `DIAMONDS`, `CLUBS`; within each suit its rank order is `3, 4, 5, 6, 7, 8, 9, 10, JACK, QUEEN, KING, ACE`. Rank 2 is absent. The canonical tuple is never shuffled in place.

## Authoritative context and commitment

For every deal, the server generates a 32-byte secret and a 16-byte nonce with `secrets.token_bytes`. It creates this canonical JSON context, encoded as UTF-8 with sorted keys and compact separators:

```json
{
  "context_version": "mendicot-shuffle-context-v1",
  "game_id": "<authoritative game ID>",
  "room_id": "<authoritative room ID>",
  "player_count": 4,
  "ordered_seats": [
    {"player_id": "P0", "seat_index": 0, "team_id": "TeamA"}
  ],
  "trump_mode": "normal",
  "selected_first_player_id": "P0",
  "selected_trump_hider_id": null,
  "nonce": "<16-byte lowercase hex>",
  "deck_definition_version": "mendicot-48-v1",
  "shuffle_algorithm_version": "hmac-sha256-fisher-yates-v1",
  "dealing_algorithm_version": "round-robin-seat-order-v1",
  "player_entropy": []
}
```

`player_count` is one of 4, 6, or 8. Seats are sorted by unique, contiguous `seat_index`; player IDs are unique; teams remain the internal IDs `TeamA` and `TeamB`. Hidden mode requires a committed trump hider. `player_entropy` is reserved for a future additive entropy design and is empty in v1.

The commitment is:

```text
SHA256(canonical_json({"context": context, "server_secret": secret.hex()}))
```

The deterministic shuffle seed is:

```text
HMAC-SHA256(key=server_secret, message=canonical_context_bytes)
```

An HMAC-SHA256 counter byte generator supplies rejection-sampled `randbelow(n)`. Explicit Fisher-Yates iterates from deck index 47 down to 1 and swaps with an unbiased index in `[0, i]`.

## Dealing and atomic commit

Cards are assigned one at a time in authoritative seat order:

```text
owner(card_index) = ordered_seats[card_index % player_count]
```

This gives 12 cards for 4 players, 8 for 6, and 6 for 8. Before live state is committed, the backend validates the exact 48-card partition, unique ownership, expected hand sizes, canonical membership, and seat configuration. Only then are hands, the `dealt` flag, `deal_generation`, locked configuration, version metadata, and commitment installed together.

A room lock serializes lobby/setup mutations, and an engine lock plus `dealt`, phase, version, and generation guards prevent duplicate or concurrent redeals. The public `DEAL_CARDS` WebSocket action is rejected; setup completion invokes the only authoritative deal path.

## Snapshot and reveal contract

During an active game, `GAME_STATE_UPDATE` contains the public commitment only:

```json
{
  "shuffle_commitment": "<sha256 hex>",
  "shuffle_audit": {
    "commitment_hash": "<sha256 hex>",
    "deck_definition_version": "mendicot-48-v1",
    "shuffle_algorithm_version": "hmac-sha256-fisher-yates-v1",
    "dealing_algorithm_version": "round-robin-seat-order-v1",
    "audit_status": "COMMITTED"
  }
}
```

It never contains the secret, nonce, deck hash, shuffled deck, or another player's hand. A hidden-trump hider receives only valid hand positions before blind selection. After trump becomes public, only its suit is public; hidden rank and original hand index remain redacted.

After the authoritative transition to `GAME_OVER` or `DRAW`, terminal verification runs in sequence:

1. `verify_shuffle_audit` reproduces the commitment, deck, deck hash, round-robin hands, context, and algorithm versions.
2. `verify_played_card_ownership` validates the complete public trick history against the reconstructed initial hands: every played card must have been originally owned by the player recorded as playing it, exactly 48 cards must appear exactly once, and no fabricated, duplicated, missing, or wrong-owner cards may exist.

If both pass, `audit_status` becomes `VERIFIED` and the revealed metadata is attached. If ownership verification fails, `audit_status` becomes `OWNERSHIP_FAILED`; the game result still completes normally so that players always reach the result screen. Secrets, hands, and deck order are never exposed in error messages or logs.


## Operations and limits

Safe logs include game/room IDs, versions, commitment hash, deck hash, player count, result category, and deal status. They exclude secrets, deck order, hands, hidden-card identity, and session tokens.

The current room/session/game architecture is in memory. A process restart loses active games and private audit records, so mid-game restart recovery and durable audit retention are not claimed. Adding persistence requires one atomic transaction for both committed game state and the private audit record.

The offline audit command treats `--deals` as deals per selected player mode. With the defaults, this runs 300,000 total deals:

```text
python -m mendicot.tools.simulate_deals --deals 100000
```

Use `--json-output <path>` to retain complete card-position, seat-card, suit/color, Mendi, invariant, verification, and timing distributions.
