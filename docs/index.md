# novelhog

![novelhog](assets/logo.png){ width="160" align=right }

A self-hosted home for the web novels you follow. Give novelhog a novel's table-of-contents URL and it
downloads the chapters, keeps checking for new ones, and exports them as EPUBs. It is a web UI and a worker
around [web-novel-scraper](https://pypi.org/project/web-novel-scraper/), shipped as one Docker image.

![The library: novels with covers, download progress and a Follow button](assets/screenshots/library-desktop.png#only-light)
![The library: novels with covers, download progress and a Follow button](assets/screenshots/library-desktop-dark.png#only-dark)

## What it does

- **Import by URL.** Paste a table-of-contents link and novelhog finds the novel, its cover and its chapters.
- **Follow and forget.** Followed novels are re-checked in the background and new chapters are downloaded
  automatically. Sites that throttle you are paused and resumed on their own.
- **Read in the app.** A reading page built for phones: swipe between chapters, pick a theme and a font
  (your own too), and reopen every chapter where you left off. See [Reader](reader.md).
- **Export EPUBs.** Split a long novel into books and export them from the novel page.
- **Installable and live.** The UI is a PWA that updates as the worker progresses, and can send Web Push
  notifications.
- **Cloudflare-protected sites.** A FlareSolverr is paired with the worker out of the box.
- **Your hardware.** SQLite by default, PostgreSQL if you prefer. Images for `amd64` and `arm64`, so a
  Raspberry Pi or a Mac mini is enough.

| The novel page | The reader |
|---|---|
| ![A novel's page with its chapter list and an exported EPUB, on a phone](assets/screenshots/novel-mobile.png#only-light) ![A novel's page with its chapter list and an exported EPUB, on a phone](assets/screenshots/novel-mobile-dark.png#only-dark) | ![The reading page on a phone](assets/screenshots/reader-mobile.png#only-light) ![The reading page on a phone](assets/screenshots/reader-mobile-dark.png#only-dark) |

## Get started

Download one file and start it:

```sh
curl -fsSLO https://raw.githubusercontent.com/t3hk0d3/novelhog/main/compose.yaml
docker compose up -d
```

Then open `http://localhost:8080`. [Install](install.md) covers the details; [Configuration](configuration.md),
[Reverse proxy](reverse-proxy.md) and [Storage and upgrades](storage.md) cover running it for real.

!!! warning "There is no login"
    Run novelhog on a trusted network or behind an authenticating reverse proxy.
