# v3 TODO: local SQLite caching layer — store collected JSON in a local db
# so re-runs can diff against prior state rather than full re-collect.
# Not in scope for v1/v2.

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as `python scripts/gather_content_inventory.py` from repo root
# by ensuring the repo root is on sys.path for `lib` imports.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.wp_ssh import load_env, parse_claude_md, ssh_connect, wp  # noqa: E402

SCRIPT_VERSION = "1.0.0"

SKIP_POST_TYPES = {
    "attachment",
    "revision",
    "nav_menu_item",
    "custom_css",
    "customize_changeset",
    "oembed_cache",
    "user_request",
    "wp_block",
}


# ---------------------------------------------------------------------------
# Step 0 — Validate siteurl
# ---------------------------------------------------------------------------

def validate_siteurl(client, wp_path: str, target_wp: str):
    siteurl = wp(client, wp_path, "option get siteurl").rstrip("/")
    expected = target_wp.rstrip("/")
    if siteurl.lower() != expected.lower():
        sys.exit(
            f"ERROR: siteurl mismatch.\n"
            f"  WP reports: {siteurl}\n"
            f"  Expected:   {expected}\n"
            f"Check target-wordpress-domain in CLAUDE.md or --site argument."
        )


# ---------------------------------------------------------------------------
# Step 1 — Discover post types
# ---------------------------------------------------------------------------

def discover_post_types(client, wp_path: str) -> tuple[list, list, list]:
    raw = wp(client, wp_path, "post-type list --fields=name,label,public,_builtin --format=json")
    try:
        all_types = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(f"ERROR: Could not parse post-type list output:\n{raw}")

    found = [pt["name"] for pt in all_types]
    included = []
    excluded = []

    for pt in all_types:
        name = pt["name"]
        if name in SKIP_POST_TYPES:
            excluded.append(name)
            continue
        is_public = str(pt.get("public", "0")) in ("1", "true", "True")

        if name not in ("post", "page") and not is_public:
            excluded.append(name)
            continue

        count_raw = wp(client, wp_path,
                       f"post list --post_type={name} --post_status=publish --format=count")
        try:
            count = int(count_raw.strip())
        except ValueError:
            count = 0

        if count > 0:
            included.append(name)
        else:
            excluded.append(name)

    return found, included, excluded


# ---------------------------------------------------------------------------
# Step 2 — Auto-detect content analysis prefix
# ---------------------------------------------------------------------------

def detect_prefix(client, wp_path: str, included_types: list) -> str:
    for post_type in included_types:
        raw = wp(client, wp_path,
                 f"post list --post_type={post_type} --post_status=publish "
                 f"--fields=ID --format=json --posts_per_page=1")
        try:
            posts = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not posts:
            continue
        post_id = posts[0]["ID"]
        val = wp(client, wp_path,
                 f"eval 'echo get_post_meta({post_id}, \"scos_ca_last_analyzed\", true);'")
        if val:
            return "scos_ca_"
        val_bw = wp(client, wp_path,
                    f"eval 'echo get_post_meta({post_id}, \"bw_last_analyzed\", true);'")
        if val_bw:
            return "bw_"
        # First post found but prefix indeterminate — default to scos_ca_
        return "scos_ca_"
    return "scos_ca_"


# ---------------------------------------------------------------------------
# Step 3 — Per-post data collection
# ---------------------------------------------------------------------------

def build_meta_php(post_id: int, prefix: str) -> str:
    return (
        "eval 'echo json_encode(array("
        f"\"word_count\"=>get_post_meta({post_id},\"{prefix}word_count\",true),"
        f"\"h2_count\"=>get_post_meta({post_id},\"{prefix}h2_count\",true),"
        f"\"image_count\"=>get_post_meta({post_id},\"{prefix}image_count\",true),"
        f"\"reading_time\"=>get_post_meta({post_id},\"{prefix}reading_time\",true),"
        f"\"links_to_internal\"=>get_post_meta({post_id},\"{prefix}links_to_internal\",true),"
        f"\"links_to_external\"=>get_post_meta({post_id},\"{prefix}links_to_external\",true),"
        f"\"last_analyzed\"=>get_post_meta({post_id},\"{prefix}last_analyzed\",true),"
        f"\"scos_ca_intent\"=>get_post_meta({post_id},\"scos_ca_intent\",true),"
        f"\"scos_ca_purpose\"=>get_post_meta({post_id},\"scos_ca_purpose\",true),"
        f"\"scos_ca_maturity\"=>get_post_meta({post_id},\"scos_ca_maturity\",true),"
        f"\"scos_ca_index_status\"=>get_post_meta({post_id},\"scos_ca_index_status\",true),"
        f"\"scos_ca_optimization_progress\"=>get_post_meta({post_id},\"scos_ca_optimization_progress\",true),"
        f"\"scos_ca_next_step\"=>get_post_meta({post_id},\"scos_ca_next_step\",true),"
        f"\"scos_seo_title\"=>get_post_meta({post_id},\"scos_seo_title\",true),"
        f"\"scos_seo_description\"=>get_post_meta({post_id},\"scos_seo_description\",true),"
        f"\"scos_seo_robots\"=>get_post_meta({post_id},\"scos_seo_robots\",true),"
        f"\"scos_seo_canonical\"=>get_post_meta({post_id},\"scos_seo_canonical\",true),"
        f"\"scos_seo_breadcrumb_title\"=>get_post_meta({post_id},\"scos_seo_breadcrumb_title\",true),"
        f"\"scos_seo_tldr\"=>get_post_meta({post_id},\"scos_seo_tldr\",true)"
        "));'"
    )


def fetch_taxonomy_term(client, wp_path: str, post_id: int, taxonomy: str) -> str:
    raw = wp(client, wp_path,
             f"post term list {post_id} {taxonomy} --fields=name --format=csv")
    lines = [l.strip() for l in raw.splitlines() if l.strip() and l.strip().lower() != "name"]
    return ", ".join(lines) if lines else ""


def or_null(val):
    if val is None or val == "":
        return None
    return val


def collect_posts(client, wp_path: str, included_types: list,
                  prefix: str, production_domain: str) -> list:
    posts_out = []
    prod_base = production_domain.rstrip("/")

    for post_type in included_types:
        raw = wp(client, wp_path,
                 f"post list --post_type={post_type} --post_status=publish "
                 f"--fields=ID,post_title,post_name,post_date --format=json")
        try:
            posts = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            print(f"WARNING: Could not parse post list for post_type={post_type}")
            continue

        for p in posts:
            post_id = int(p["ID"])
            slug = p.get("post_name", "")

            meta_cmd = build_meta_php(post_id, prefix)
            meta_raw = wp(client, wp_path, meta_cmd)
            try:
                meta = json.loads(meta_raw)
            except (json.JSONDecodeError, TypeError):
                meta = {}

            cluster = fetch_taxonomy_term(client, wp_path, post_id, "scos_content_cluster")
            topic = fetch_taxonomy_term(client, wp_path, post_id, "scos_topic")

            last_analyzed = meta.get("last_analyzed", "")
            analysis_status = "complete" if last_analyzed else "pending"

            production_url = f"{prod_base}/{slug}/" if slug else None
            gsc_url = (
                f"https://search.google.com/search-console/performance/"
                f"search-analytics?resource_id={production_url}"
                if production_url else None
            )
            ga4_path = f"/{slug}/" if slug else None

            def strat(key):
                if prefix == "bw_":
                    return None
                return or_null(meta.get(key))

            posts_out.append({
                "id": post_id,
                "title": p.get("post_title", ""),
                "slug": slug,
                "post_type": post_type,
                "post_date": p.get("post_date", ""),
                "analysis_status": analysis_status,
                "word_count": or_null(meta.get("word_count")),
                "h2_count": or_null(meta.get("h2_count")),
                "image_count": or_null(meta.get("image_count")),
                "reading_time": or_null(meta.get("reading_time")),
                "internal_link_count": or_null(meta.get("links_to_internal")),
                "external_link_count": or_null(meta.get("links_to_external")),
                "last_analyzed": or_null(last_analyzed),
                "scos_ca_intent": strat("scos_ca_intent"),
                "scos_ca_purpose": strat("scos_ca_purpose"),
                "scos_ca_maturity": strat("scos_ca_maturity"),
                "scos_ca_index_status": strat("scos_ca_index_status"),
                "scos_ca_optimization_progress": strat("scos_ca_optimization_progress"),
                "scos_ca_next_step": strat("scos_ca_next_step"),
                "scos_seo_title": or_null(meta.get("scos_seo_title")),
                "scos_seo_description": or_null(meta.get("scos_seo_description")),
                "scos_seo_robots": or_null(meta.get("scos_seo_robots")),
                "scos_seo_canonical": or_null(meta.get("scos_seo_canonical")),
                "scos_seo_breadcrumb_title": or_null(meta.get("scos_seo_breadcrumb_title")),
                "scos_seo_tldr": or_null(meta.get("scos_seo_tldr")),
                "cluster": or_null(cluster),
                "topic": or_null(topic),
                "production_url": production_url,
                "gsc_url": gsc_url,
                "ga4_path": ga4_path,
            })

    return posts_out


# ---------------------------------------------------------------------------
# Step 5 — Write output
# ---------------------------------------------------------------------------

def write_output(site: str, base_dir: str, payload: dict) -> str:
    out_dir = Path(base_dir) / site / "data"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        sys.exit(f"ERROR: Output folder not writable: {out_dir}")

    out_file = out_dir / "content-inventory.json"
    try:
        out_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except PermissionError:
        sys.exit(f"ERROR: Cannot write to {out_file} — permission denied")

    return str(out_file)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Gather WordPress content inventory via SSH/WP-CLI."
    )
    parser.add_argument("--site", required=True,
                        help="Site slug, e.g. brighter-websites")
    args = parser.parse_args()
    site = args.site

    env = load_env()
    config = parse_claude_md(site)

    print(f"Connecting to {env['SSH_HOST']} …")
    client = ssh_connect(env)
    validate_siteurl(client, env["WP_PATH"], config["target_wordpress_domain"])

    print("Discovering post types …")
    found, included, excluded = discover_post_types(client, env["WP_PATH"])
    print(f"  Included: {included}")

    prefix = detect_prefix(client, env["WP_PATH"], included)
    print(f"  Content analysis prefix: {prefix}")

    print("Collecting posts …")
    posts = collect_posts(
        client, env["WP_PATH"], included, prefix, config["production_domain"],
    )
    client.close()

    total = len(posts)
    pending_count = sum(1 for p in posts if p["analysis_status"] == "pending")
    complete_count = total - pending_count
    pending_pct = (pending_count / total * 100) if total else 0.0

    if total > 0 and pending_count / total > 0.20:
        print(
            f"\nWARNING: {pending_count}/{total} posts ({pending_pct:.0f}%) have no analysis data.\n"
            f"Run the SCOS content analysis tool to backfill before using this inventory.\n"
        )

    collected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "meta": {
            "site": site,
            "production_domain": config["production_domain"],
            "staging_mode": config["staging_mode"],
            "collected_at": collected_at,
            "task": "gather-content-inventory-py",
            "script_version": SCRIPT_VERSION,
            "content_analysis_prefix": prefix,
            "wp_post_types_found": found,
            "wp_post_types_included": included,
            "wp_post_types_excluded": excluded,
            "total_posts_included": total,
            "analysis_complete_count": complete_count,
            "analysis_pending_count": pending_count,
        },
        "posts": posts,
    }

    out_path = write_output(site, config["base_dir"], payload)

    print(
        f"\ngather_content_inventory.py complete\n"
        f"Site: {site}\n"
        f"Target WP: {config['target_wordpress_domain']}\n"
        f"Posts collected: {total}\n"
        f"Analysis pending: {pending_count} ({pending_pct:.0f}%)\n"
        f"Content analysis prefix: {prefix}\n"
        f"File written: {out_path}\n"
        f"Next: run Task A (GSC/GA4) or Task B if traffic-signals.json already exists"
    )


if __name__ == "__main__":
    main()
