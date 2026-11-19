# Installing Shannon

From this repository to a bot that mirrors your GitHub repository into your Discord server (#242).
Follow the steps in order. The [README](README.md) explains what each part does, and its
[Configuration](README.md#configuration) section lists every setting.

## What you need

- A GitHub account with admin access to the repository you want to mirror.
- A Discord server where you can add bots (the Manage Server permission).
- Docker, with Docker Compose 2.24 or newer (`docker compose version` shows it). Both compose files
  use a form of `env_file` that older versions reject.
- To run it for real: a Linux server you can reach as root, with `git`, `curl` and `jq` installed,
  and a domain name you can point at it.
- A shell with `openssl` and `awk`, such as Linux, macOS, Git Bash or WSL, for the commands that
  make secrets and keys. PowerShell alternatives are given where they differ.
- To mirror a GitHub project board: the person who owns the board, to authorise it once.

Two addresses come up again and again below. Decide them first:

- **The public URL** is the `https://` address where GitHub and Discord reach the bot, for example
  `https://shannon.example.com`. It goes into `SHANNON_PUBLIC_BASE_URL`, with no slash at the end.
  On a server it is your domain. On a laptop it is a tunnel's address: start the tunnel first
  (see [Try it on your laptop](#5b-try-it-on-your-laptop)), so you have the address.
- **The `.env` file** holds every secret. Start it with `cp .env.example .env`. It is ignored by
  git, so never commit it. `.env.example` explains each setting in a comment. Compose reads `$` in
  `.env` as the start of a variable, so make every secret you invent with `openssl rand -hex 32`,
  which uses only letters and digits.

## 1. Make the Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create a
   new application.
2. Under **Bot**, reset the token and copy it into `SHANNON_DISCORD_TOKEN`.
3. Leave the privileged intents off. The bot needs none, unless you want `/log_conversation`: then
   tick **Message Content** here first, and only then set `SHANNON_CAPTURE_DISCORD_MESSAGES=true`.
   The other order stops the bot at startup.
4. Under **OAuth2**:
   1. Add the redirect `<public URL>/oauth/discord/callback`, exactly.
   2. Copy the Client ID into `SHANNON_DISCORD_CLIENT_ID`.
   3. Reset the Client Secret and copy it into `SHANNON_DISCORD_CLIENT_SECRET`. This does not touch
      the bot token.
   4. Leave **Public Client** off.

The OAuth2 part is the Discord sign-in. Every one-time link the bot hands out (`/link`,
`/register`, `/unregister`, `/board`) asks Discord who is holding the browser before it asks
GitHub, so a link only works for the member who ran the command. Without these settings, every
command that hands out a link refuses.

## 2. Make the GitHub App

The App is how the bot reads your repository and receives its events. Create it under your GitHub
**Settings**, **Developer settings**, **GitHub Apps**, **New GitHub App**.

1. **Name and homepage.** Any name. The homepage can be this repository's URL.
2. **Callback URL:** `<public URL>/oauth/github/callback`. The sign-in for `/link`, `/register` and
   `/unregister` comes back here.
3. **Webhook:** active, with the URL `<public URL>/webhooks/github`. Make a secret with
   `openssl rand -hex 32`, put it in the App's **Webhook secret** field, and copy the same string
   into `SHANNON_GITHUB_APP_WEBHOOK_SECRET`.
4. **Repository permissions.** These three, and nothing else:
   - `Issues`: Read and write. For labels, assignees and comments.
   - `Pull requests`: Read and write. For reviewers.
   - `Checks`: Read-only. For the CI results posted in a thread (#112).
5. **Subscribe to events.** These six. GitHub's App page names some of them in the singular, such
   as Pull request and Issue comment.

   ```text
   Pull requests
   Issues
   Issue comments
   Pull request reviews
   Pull request review comments
   Check suites
   ```

   The bot answers any other event `ignored` and stores nothing, so more events only add noise.
6. **Where it can be installed.** "Only on this account" is enough when every repository you will
   mirror belongs to you. Choose "Any account" if repositories owned by other accounts or
   organisations will be registered.
7. Create the App. Then, on its settings page:
   1. Copy the **Client ID** into `SHANNON_GITHUB_APP_CLIENT_ID`.
   2. Generate a **client secret** and copy it into `SHANNON_GITHUB_APP_CLIENT_SECRET`.
   3. Generate a **private key**. GitHub downloads a `.pem` file. Put it in
      `SHANNON_GITHUB_APP_PRIVATE_KEY` on one line, with each newline written as `\n`:

      ```bash
      awk 'NF {sub(/\r/, ""); printf "%s\\n", $0}' your-app.private-key.pem
      ```

      In PowerShell: `(Get-Content your-app.private-key.pem) -join '\n'`.

      Keep the `.pem` file safe. It can act for every installation of the App.
8. **Install the App** on the repository you want to mirror (the App's page, **Install App**).

Leave `SHANNON_GITHUB_WEBHOOK_SECRET` empty. It is only for an old per-repository webhook that is
still live while a deployment moves to the App. Do not add a per-repository webhook as well: GitHub
would send every event twice, and every commit line would be posted twice.

Adding a permission to the App later pauses its event delivery until somebody accepts the change
on the installation. Until then the new feature looks broken rather than pending.

## 3. Optional: project boards

Skip this unless you want a GitHub project board mirrored. A board needs a second, separate
application, because GitHub has no App permission for a board owned by a person.

1. Register a classic **OAuth App** (not a GitHub App) under **Settings**, **Developer settings**,
   **OAuth Apps**, **New OAuth App**:
   - Authorization callback URL: `<public URL>/oauth/github/callback`, exactly.
   - Enable Device Flow: off.
   - If GitHub offers **Expire user access tokens**, leave it off. With it on, a board stops being
     read eight hours after it was linked.
2. Copy its Client ID into `SHANNON_GITHUB_BOARD_CLIENT_ID`, and generate a client secret for
   `SHANNON_GITHUB_BOARD_CLIENT_SECRET`.
3. Generate the key that encrypts board authorisations, and put it in
   `SHANNON_BOARD_CREDENTIAL_KEY`:

   ```bash
   openssl rand -base64 32 | tr '+/' '-_'
   ```

   That is 32 random bytes in URL-safe base64, which is the form the key takes. With Docker only:
   `docker run --rm python:3.15-slim python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`.

A board owned by an organisation also needs the organisation to approve this OAuth App. GitHub's
sign-in page shows a **Grant** button to an owner and a **Request** button to anybody else.

## 4. Fill in `.env`

By now `.env` has these. The rest of `.env.example` has working defaults.

| For | Settings |
| --- | --- |
| The Discord bot | `SHANNON_DISCORD_TOKEN`, `SHANNON_DISCORD_CLIENT_ID`, `SHANNON_DISCORD_CLIENT_SECRET` |
| The GitHub App | `SHANNON_GITHUB_APP_CLIENT_ID`, `SHANNON_GITHUB_APP_PRIVATE_KEY`, `SHANNON_GITHUB_APP_CLIENT_SECRET`, `SHANNON_GITHUB_APP_WEBHOOK_SECRET` |
| One-time links | `SHANNON_PUBLIC_BASE_URL` |
| Project boards, optional | `SHANNON_GITHUB_BOARD_CLIENT_ID`, `SHANNON_GITHUB_BOARD_CLIENT_SECRET`, `SHANNON_BOARD_CREDENTIAL_KEY` |
| A server only | `SHANNON_HOSTNAME`, `ACME_EMAIL`, `POSTGRES_USER`, `POSTGRES_PASSWORD` |

The role names the bot looks for are `SHANNON_ROLE_ADMIN`, `SHANNON_ROLE_PROJECT_MANAGER`,
`SHANNON_ROLE_REVIEWER` and `SHANNON_ROLE_DEVELOPER`. They default to Admin, Project Manager,
Reviewer and Developer. Change them if your server calls its roles something else; each takes a
comma-separated list.

## 5. Start it

Choose one. A server is the way to run it for real. A laptop is for trying it out.

### 5a. On a server

The server runs the image CI built, behind Caddy, which gets an HTTPS certificate and closes the
API documentation pages.

1. Point a DNS A record for your domain at the server. Without it, Caddy cannot get a certificate.
2. On the server, as root, clone this repository into `/opt/shannon`, and run every later step
   there:

   ```bash
   git clone https://github.com/Canon-Regularis/Shannon-bot.git /opt/shannon
   cd /opt/shannon
   ```

   Keep the directory name `shannon`. Compose names the database volume after it, and
   `scripts/deploy.sh` refuses any other path or name.
3. Write `.env` there (step 4). Set `SHANNON_HOSTNAME` to your domain, `ACME_EMAIL` to an address
   for the certificate, and real values for `POSTGRES_USER` and `POSTGRES_PASSWORD`. Use only
   letters and digits in both, for example `openssl rand -hex 32` for the password: the compose
   file puts them into the database URL as they are, so an `@` or `%` breaks the connection.
   Postgres reads those two only once, when it creates its volume, so choose them before the first
   start. Changing them later means removing the database volume.
4. Log in to the registry and start the stack:

   ```bash
   echo "$GHCR_TOKEN" | docker login ghcr.io -u <user> --password-stdin   # classic PAT, read:packages
   docker compose -f compose.prod.yaml up -d
   ```

   The stack runs the database migrations to completion before the bot starts.
5. Check it: `curl https://<your domain>/health` should answer with `"healthy": true`.

Run one copy only. Two copies would both connect to Discord and both work the same queue.

### 5b. Try it on your laptop

The laptop stack builds the image from your checkout and runs it on port 8000, with no Caddy.

1. Start an HTTPS tunnel to `http://localhost:8000` with any tunnelling tool. This repository
   ships none. The tunnel's `https://` address is your public URL: put it in
   `SHANNON_PUBLIC_BASE_URL`, and use it in the Discord redirect, the App's webhook and callback
   URLs, and the board OAuth App's callback URL if you made one (all above). If the tunnel's address
   changes each time it starts, update all of them each time, then run `docker compose up -d`
   again.
2. Start the stack:

   ```bash
   docker compose up -d --build
   ```

   The database migrations run to completion first, then the bot starts.
3. Check it: `curl http://localhost:8000/health`.
4. Stop it with `docker compose down`. The database stays in its volume for next time.

Without Caddy in front, the tunnel also serves `/docs`, `/redoc` and `/openapi.json` to anyone
who has the address. Use this only to try the bot out.

## 6. Invite the bot to your server

In the Developer Portal, under **OAuth2**, **URL Generator**, pick the `bot` and
`applications.commands` scopes and these permissions, then open the URL it builds:

```text
View Channels
Send Messages
Send Messages in Threads
Create Public Threads
Manage Threads
Read Message History
```

Two of these are easy to miss:

- **Read Message History** lets the bot edit its own summary message in a thread. Without it, a
  thread appears once and then never changes.
- **Manage Threads** lets the bot lock and archive a finished item's thread, and reopen it when
  something new arrives. Grant it before anything closes.

The bot's commands appear globally, and the first time Discord can take up to an hour to show
them. Until then, typing `/` shows nothing, and nothing is wrong. Restarting the Discord app often
shortens the wait.

## 7. Set it up in Discord

Run these in your server. Only `/register` has to come first.

```text
/register <github_repo_link>          in the PR channel: once to prove you administer it, again to bind it
/set_channel issues #channel          where issue threads go
/board link <board>                   only if a board is being mirrored: one link, one click
/set_channel project tickets #channel and where its tickets go
/link                                 each person, once: open the link and that is it
/linked                               any time after, to see that it took
/link_team <team> @role               so a review asked of a team reaches somebody
```

- `/register` needs the Admin or Project Manager role, and a GitHub account with admin on the
  repository. The channel you run it in becomes the pull request channel.
- Issues use the pull request channel until you give them their own.
- A board's tickets have no fallback: the board is not mirrored until `/set_channel project tickets`
  names a channel.
- `/link` connects each person's GitHub account to their Discord account, so the bot can ping
  them. Everybody runs it once.
- The role given to `/link_team` needs **Allow Anyone To @mention This Role** turned on in its
  settings, or the ping reaches nobody.

The [Commands](README.md#commands) section of the README lists every command and the role it needs.

## 8. Check that it works

1. `curl <public URL>/health` answers `"healthy": true`. On a server, `version` names the commit
   running. The laptop stack builds its own image, so there it says `unknown`, and that is
   expected.
2. Open an issue or a pull request in the repository. A thread appears in its channel within a few
   seconds.
3. If nothing appears, open the App's **Advanced** settings on GitHub and look at **Recent
   Deliveries**. The table below says what the answer means.

## When something is wrong

A change to `.env` reaches the bot only when its container is recreated: run
`docker compose -f compose.prod.yaml up -d` on a server, or `docker compose up -d` on a laptop.
`docker compose restart` keeps the old values.

| What you see | Why, and what to do |
| --- | --- |
| Recent Deliveries shows **401** | The webhook secret does not match. Copy the App's secret into `SHANNON_GITHUB_APP_WEBHOOK_SECRET` again. |
| Recent Deliveries shows **500** | Neither webhook secret is set in `.env`. |
| **200** answering `ignored` | The event arrived, and the bot does not act on that kind of event. |
| Typing `/` shows no Shannon commands | Discord's command cache. Wait up to an hour after the first start. |
| The bot exits right after starting | Often a token left at its placeholder from `.env.example`. Check `SHANNON_DISCORD_TOKEN`. |
| A thread appeared once and never changes | The bot lacks **Read Message History** in that channel. |
| Finished items' threads stay open | The bot lacks **Manage Threads**. Adding it later is enough. |
| Every command that hands out a link refuses | `SHANNON_PUBLIC_BASE_URL`, the GitHub App's client id or secret, or the Discord client id and secret are not set. The command's reply names what is needed. |
| `/register` refuses with a link | The App is not installed on that repository. Follow the link to install it. |
| Every event, and every commit line, arrives twice | A per-repository webhook is still set up next to the App's. Delete the per-repository one. |
| Something merged does not seem to work | On a server, `/health` reports the commit running, and the fix may simply not be deployed yet. On a laptop, rebuild with `docker compose up -d --build`. |

## Keeping it up to date

On a server, deploy a new version from your own machine with one command:

```bash
ssh root@<host> /opt/shannon/scripts/deploy.sh              # the head of main
ssh root@<host> /opt/shannon/scripts/deploy.sh <commit>     # a named commit
ssh root@<host> /opt/shannon/scripts/deploy.sh --rollback   # the previous build
```

It refuses a commit CI never built an image for, and puts the previous image back if the new one
never reports healthy. [scripts/README.md](scripts/README.md) has the whole procedure.

To add the bot to another Discord server, invite it there (step 6), install the App on that
server's repository, and run step 7 in the new server. One running bot serves every server it is
in: see [One bot, several servers](README.md#one-bot-several-servers).
