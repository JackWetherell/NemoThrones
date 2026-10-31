<!-- nemothrones:begin -->
## NemoThrones (game)

NemoThrones lives in `/sandbox/.hermes/workspace/nemothrones/`: the game venv,
saves, `lore.db` (the WOIAF cache) and `nemothrones.yaml`. It is on the persistent
volume, so `hermes --backup` covers it.

When the engine calls you through the API as the GAME MASTER or the DIRECTOR,
follow the `nemothrones-gm` skill: reply with one JSON object, use the read-only
`nemothrones` plugin tools for facts, keep to book canon, and never reveal fate.
For lore research, follow `woiaf-maester`.

Never write to NemoThrones saves. The engine is the only writer; the plugin opens
saves read-only.
<!-- nemothrones:end -->
