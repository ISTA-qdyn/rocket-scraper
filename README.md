# Rocket.Chat archiver

A single Python script that saves your Rocket.Chat conversations to your own
computer before the server is shut down. It saves the messages and every
attached file (pdf, png, and so on).

It uses only the Python standard library, so there is nothing to install. It
logs in as you and can only reach the rooms you are already allowed to see. Each
person runs it with their own account, so it is safe to share.

## What you get

For every room, a folder under `rocket_archive/`:

```
rocket_archive/
  index.json                      list of all rooms with message/file counts
  channel_steering/
    messages.json                 full data, one entry per message
    chat.html                     readable transcript, images shown inline
    files/                        every uploaded pdf, png, ... from that room
  group_my-private-group/
  direct_dm_alice_bob/
```

Open `chat.html` in any browser to read a conversation. Use `messages.json` if
you want to process the text later.

## Browse the archive

After exporting, build a single-page viewer:

```
python3 build_viewer.py            # reads ./rocket_archive
```

This writes `rocket_archive/viewer.html`. Double-click it to open a browser with
a sidebar of every room (channels, groups, direct messages) and a search box;
click a room to read the conversation, with images shown inline and other files
linked. Keep `viewer.html` where it is created, next to the room folders, since
it points at the downloaded files by relative path.

Each room folder also has its own `chat.html` you can open on its own.

## Requirements

Python 3.8 or newer. Check with:

```
python3 --version
```

macOS and Linux already have it. On Windows, install from python.org.

## Run it (use a personal access token)

On the ISTA server, plain username-and-password login does not give the script
access to your conversations: it authenticates, but the private groups and
direct messages come back empty, so only a public channel or two show up. You
must use a personal access token instead. The token carries your full access, so
your groups and DMs are visible.

1. In Rocket.Chat, click your avatar -> **My Account** -> **Personal Access
   Tokens**.
2. Create a token (you may need to enter your 2FA code). Copy the **token** and
   the **user id** shown next to it. Both are needed.
3. Run:

```
python3 rocket_export.py --server https://chat.ista.ac.at \
    --user-id YOUR_USER_ID --token YOUR_TOKEN
```

Treat the token like a password: anyone who has it can act as you. Do not put it
in a shared file or email. When you have finished archiving, delete the token on
the same Personal Access Tokens page.

### Password login (usually not enough)

The script also accepts a username and password (and a 2FA code if your account
uses two-factor):

```
python3 rocket_export.py --server https://chat.ista.ac.at
```

Leave this for a server where it actually works. On ISTA it logs in but cannot
see your groups and direct messages, so use the token method above.

## Options

- `--out FOLDER` write the archive somewhere other than `rocket_archive`.
- `--skip-files` save only the text, skip downloading attachments (much faster).
- `--user NAME` supply the username on the command line (still prompts for the
  password).

## Notes and limits

- It archives what your account can see: public channels you have joined,
  private groups you belong to, and your direct messages. It does not reach
  channels you never joined.
- Large rooms with many files take a while. The server may briefly slow the
  script down (rate limiting); it waits and retries on its own.
- If a single file fails to download, the transcript marks it as
  "not downloaded" and the run continues.
- Re-running overwrites the previous archive in the same output folder.

## Sharing with colleagues

Send them this folder (`rocket_export.py` and `README.md`). Each person creates
their own personal access token and runs the token command above, getting their
own archive of their own rooms. No admin rights and no server access are needed.
Tokens are personal, so nobody should share a token with anyone else.
