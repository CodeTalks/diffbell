<!-- Generated from src/diffbell/web/static/guide.json by scripts/gen_guide.py: edit the JSON, then run `make guide`. -->

# Set up Telegram for Diffbell

Diffbell sends its notifications through a Telegram bot that belongs to you. Creating one takes about two minutes and needs only the Telegram app.

Settings → Telegram in the Diffbell page walks you through the four steps below. In a terminal, diffbell setup does the same.

## 1. Create a bot

Open a chat with @BotFather, Telegram's official bot for making bots (it has a blue verified badge), and send /newbot. BotFather asks for two things.

A name: what the chat list shows, for example Diffbell. Any text works, and /setname changes it later.

A username: the bot's address. It must be unique on Telegram, 5 to 32 characters long, use only Latin letters, digits and underscores, and end in "bot", for example my\_diffbell\_bot or MyDiffbellBot. It can't be changed later.

Use a new bot just for Diffbell, not one that already serves something else: Diffbell reads the bot's messages once, during setup, to find your chat.

- [Open @BotFather](https://t.me/BotFather)

## 2. Copy the token

When the username is accepted, BotFather replies with a token: a number, a colon, then about 35 letters, digits, dashes and underscores, like 123456789:AAH4… (yours is longer).

Copy the whole token, paste it into the Token field in Settings → Telegram and click Check. Diffbell asks Telegram which bot it belongs to and shows the bot's @username.

The token is the bot's password: whoever has it can send messages as your bot and read what is sent to it. Don't paste it into a chat, an issue or a screenshot. Diffbell keeps it only in your Mac's Keychain, never in its config file or its logs, and the page never shows it again.

If the token leaks, send /revoke to BotFather, pick the bot, and set up again with the new token.

## 3. Send /start to your bot

After clicking Check, open your bot in Telegram (search for its @username, or tap the link in BotFather's reply) and press Start, or send /start.

Only a message sent after you clicked Check counts. This keeps anyone who messaged the bot earlier from being picked as the chat. If you sent /start before clicking Check, send it again.

The page looks for your message every few seconds for two minutes and selects your chat when it arrives. If more than one person messaged the bot since, pick yourself by name.

If you already know the chat id, you can enter it by hand instead.

## 4. Send a test message and save

Click Send test message: a sample notification arrives in the chat, so you see what a real one looks like.

Then click Save. Diffbell stores the token and the chat id in the Keychain and starts sending at once. Changes recorded while Telegram wasn't set up arrive as one catch-up message per project.

## Using Diffbell on more than one Mac

Several Macs can share one bot and one chat. On each Mac, set up Telegram with the same bot token.

The bot already has your chat, so after clicking Check send /start to the bot again: the page finds the chat as it did the first time. Or enter the chat id by hand.

Each Mac signs its messages with its name: a notification's first line ends with 💻 and the name, and alerts and the daily heartbeat read Diffbell (name). The name is the Mac's Computer Name; set another one, or turn it off, in Settings → Notifications.

Each Mac numbers its own changes, so the Open link at the end of a notification names its Mac too: it opens that Mac's Diffbell page, on that Mac.

## Privacy

The bot only ever sends to the one chat you picked. It never answers anyone else: strangers can find a bot and message it, but they get no reply and see nothing of yours.

Notifications carry file names and short diffs of your key files. Files on the redaction list (.env files, keys, certificates, credentials) are named, but their contents are never sent.

## Troubleshooting

### That doesn't look like a bot token

The pasted text isn't a whole token. A token looks like digits, a colon, then a long code, for example 123456789:AAH4… (yours is longer). Copy it again from @BotFather, all of it, and click Check again.

### Telegram rejected this token

The token looks right, but Telegram doesn't know it: it was mistyped or revoked. Send /token to BotFather and pick the bot to see the current one, copy it whole, and click Check again.

### No chat is found

Send /start to the bot after clicking Check, then click Check again: a message sent before Check doesn't count.

If the bot has a webhook, or another program reads its updates, Diffbell can't see its messages and asks for the chat id instead. Create a new bot for Diffbell, or enter the chat id.

### Notifications stopped after they worked

If the token was revoked (/revoke in BotFather) or the bot was deleted, Telegram refuses it. Open Settings → Telegram, click Replace bot and set up with a new token; until you save, Diffbell keeps the old one.

If you blocked the bot, open its chat in Telegram and press Restart.

### An earlier save didn't finish

A save stopped before both items reached the Keychain, so Diffbell doesn't trust what is there and holds notifications. Set up again in Settings → Telegram, or run diffbell setup.

### Set up from a terminal

diffbell setup asks for the same token and chat in a terminal and writes the same Keychain items. A Diffbell that is already running keeps its previous bot (or none) until you run diffbell install.
