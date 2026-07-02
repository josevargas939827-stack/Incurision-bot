# Spoils of War - Arcadion Raid Bot

Discord bot for persistent PvE raids against Arcadion.

## Installation

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and add `DISCORD_TOKEN`. If you set `GUILD_ID`, slash commands sync only to that server during development.

## Run

```powershell
py -3 -m arcadion_bot
```

## Commands

### Players

- `/army_set bulls rhinos lieutenants generals mechas`: register or update your permanent army.
- `/army_view user`: show a player's permanent army.
- `/raid_join bulls rhinos lieutenants generals mechas`: send troops to the recruiting raid.
- `/attack`: attack during the battle.
- `/raid_status`: show corruption, participants, remaining power, and top damage.
- `/modifier_use modifier`: use an available modifier.

### Administration

Requires `Manage Guild` permission.

- `/arcadion_create name city level max_corruption duration_hours arcadion_bulls arcadion_rhinos arcadion_lieutenants arcadion_generals arcadion_mechas`: create a raid in recruitment and optionally give Arcadion a corrupted army.
- `/raid_start`: start the battle.
- `/modifier_apply user modifier`: apply an admin modifier like Fallen Lieutenant.
- `/raid_finish`: manually finish the active raid.

## Model

SQLite stores permanent armies, raids, participants, losses, attacks, and modifiers. Game logic lives outside the storage layer so a future PostgreSQL migration stays straightforward.
## Arcadion Corrupted Army

Arcadion can be created with Bulls, Rhinos, Lieutenants, Generals, and Mechas. During player attacks, damage destroys Arcadion's corrupted army first when the damage can pay the unit value. Only leftover damage reduces corruption.

Example: 12,000 player damage against Arcadion's guard can destroy 1 Lieutenant and 4 Bulls for 11,000 power, then 1,000 damage is left for corruption.