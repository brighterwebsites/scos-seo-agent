# scos-gather

Python scripts for the **Brighter Websites SEO Command Center** (SCOS — Strategic Content Operating System). This repo collects raw WordPress data so Claude can perform SEO analysis without making dozens of individual WP-CLI calls per session.

## Repo structure

```
scos-gather/
├── scripts/          # Data-gathering scripts (one per data source)
├── lib/              # Shared Python utilities (SSH, WP-CLI, env, config)
├── skills/           # Claude Code skill definitions (SKILL.md per skill)
├── agents/           # Subagent prompt/definition markdown files
├── schema/           # Schema.org JSON-LD templates
├── .env.example      # SSH credential template
├── requirements.txt
└── README.md
```

---

## scripts/gather_content_inventory.py

### Purpose

Replaces the iterative WP-CLI steps in **Task A (Steps 1–4)** by running all WordPress post/meta/taxonomy collection in a single execution and writing a complete `content-inventory.json` to your local machine.

Claude reads that file once instead of issuing one WP-CLI command per post.

### How it fits Task A

```
Task A workflow
│
├─ gather_content_inventory.py   ← this script (Steps 1–4)
│   Collects: post list, all meta, taxonomies
│   Writes:   [site]/data/content-inventory.json
│
├─ GSC / GA4 collection          ← separate Task A step (traffic data)
│   Writes:   [site]/data/traffic-signals.json
│
└─ Task B analysis               ← Claude reads both JSON files
```

### Prerequisites

- Python 3.11+
- WP-CLI installed on the remote WordPress server
- SSH key-based access to that server
- A `CLAUDE.md` file for each site at:
  `C:\Users\vanes\Desktop\seo-command-center\[site]\CLAUDE.md`

  The file must contain these fields (anywhere in the document):

  ```
  target-wordpress-domain: https://staging.example.com
  production-domain: https://example.com
  staging-mode: true
  ```

### Installation

```bash
pip install -r requirements.txt
```

### .env setup

Copy `.env.example` to `.env` in the repo root and fill in your values:

```
SSH_HOST=your.server.hostname.com
SSH_USER=ssh_username
SSH_KEY_PATH=C:\Users\vanes\.ssh\id_rsa
WP_PATH=/var/www/html/wordpress
```

`SSH_KEY_PATH` accepts RSA or Ed25519 private keys.

### How to run

```bash
python scripts/gather_content_inventory.py --site brighter-websites
```

Output is written to:
```
C:\Users\vanes\Desktop\seo-command-center\brighter-websites\data\content-inventory.json
```

The `data\` folder is created automatically if it does not exist.

### Output schema

`content-inventory.json` has two top-level keys:

- **`meta`** — run metadata: site, domain, timestamps, post type lists, analysis counts, detected prefix
- **`posts`** — array of post objects, one per published post across all included post types

Each post object includes: `id`, `title`, `slug`, `post_type`, `post_date`, `analysis_status`, word/heading/image counts, reading time, link counts, `last_analyzed`, all `scos_ca_*` strategy and SEO meta fields, `cluster`, `topic`, `production_url`, `gsc_url`, `ga4_path`.

### Environment variable override (dev/CI)

To use a non-Windows base path, set:

```bash
SCOS_BASE_DIR=/path/to/seo-command-center
```

The script will look for `CLAUDE.md` and write output relative to that path instead.

### Troubleshooting

| Error | Fix |
|---|---|
| `.env missing required key(s)` | Fill in all four keys in `.env` |
| `CLAUDE.md not found` | Check `--site` matches your folder name exactly |
| `SSH authentication failed` | Verify `SSH_USER`, `SSH_KEY_PATH`; ensure key is authorised on server |
| `siteurl mismatch` | Check `target-wordpress-domain` in `CLAUDE.md` matches `wp option get siteurl` |
| `WP-CLI not found` | Confirm `wp` is on `$PATH` for the SSH user on the server |
| `>20% posts analysis pending` | Run SCOS content analysis backfill before Task B |
