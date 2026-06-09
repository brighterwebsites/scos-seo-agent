# Gathers a full WordPress content inventory in ONE server-side pass.
#
# Performance note: earlier versions issued ~3 `wp` calls per post (meta + two
# taxonomy lookups). Every `wp` call boots the entire WordPress stack on the
# server, so a ~380-post site meant ~1,140 bootstraps over SSH — tens of
# minutes. This version pipes a single PHP gatherer to `wp eval-file -`, which
# boots WordPress ONCE, loops every post server-side, and returns the whole
# inventory in a single round-trip (seconds).
#
# v3 TODO: local SQLite caching layer + `--since` incremental collection.

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running as `python scripts/gather_content_inventory.py` from repo root.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.wp_ssh import load_env, parse_claude_md, ssh_connect, wp, wp_eval_stdin  # noqa: E402

SCRIPT_VERSION = "2.0.0"

# Internal/builtin post types never worth inventorying.
SKIP_POST_TYPES = [
    "attachment",
    "revision",
    "nav_menu_item",
    "custom_css",
    "customize_changeset",
    "oembed_cache",
    "user_request",
    "wp_block",
]


# ---------------------------------------------------------------------------
# Server-side gatherer (runs inside one WordPress bootstrap)
# ---------------------------------------------------------------------------
# Tokens __PROD_BASE__ and __SKIP_LIST__ are substituted before sending.
# Output schema / field order matches the previous per-post implementation so
# the final JSON is byte-stable (taxonomy now uses wp_get_object_terms instead
# of CSV parsing, which removes stray quoting on term names).
_PHP_GATHER = r"""<?php
$prod_base = rtrim('__PROD_BASE__', '/');
$skip = array(__SKIP_LIST__);

$all = get_post_types(array(), 'objects');
$found = array(); $included = array(); $excluded = array();
foreach ($all as $name => $obj) { $found[] = $name; }
foreach ($all as $name => $obj) {
    if (in_array($name, $skip, true)) { $excluded[] = $name; continue; }
    $is_public = !empty($obj->public);
    if ($name !== 'post' && $name !== 'page' && !$is_public) { $excluded[] = $name; continue; }
    $counts = wp_count_posts($name);
    $cnt = isset($counts->publish) ? (int) $counts->publish : 0;
    if ($cnt > 0) { $included[] = $name; } else { $excluded[] = $name; }
}

$prefix = 'scos_ca_';
foreach ($included as $pt) {
    $q = get_posts(array('post_type'=>$pt,'post_status'=>'publish','numberposts'=>1,'fields'=>'ids','suppress_filters'=>true));
    if (empty($q)) { continue; }
    $pid = $q[0];
    if (get_post_meta($pid, 'scos_ca_last_analyzed', true)) { $prefix = 'scos_ca_'; }
    elseif (get_post_meta($pid, 'bw_last_analyzed', true)) { $prefix = 'bw_'; }
    else { $prefix = 'scos_ca_'; }
    break;
}

function _nn($v) { return ($v === '' || $v === null) ? null : $v; }

$is_bw = ($prefix === 'bw_');
$posts_out = array();
foreach ($included as $pt) {
    $items = get_posts(array('post_type'=>$pt,'post_status'=>'publish','numberposts'=>-1,'suppress_filters'=>true));
    foreach ($items as $post) {
        $pid = $post->ID;
        $slug = $post->post_name;

        $last_analyzed = get_post_meta($pid, $prefix . 'last_analyzed', true);
        $analysis_status = $last_analyzed ? 'complete' : 'pending';

        $cl = wp_get_object_terms($pid, 'scos_content_cluster', array('fields'=>'names'));
        $cluster = (!is_wp_error($cl) && !empty($cl)) ? implode(', ', $cl) : '';
        $tp = wp_get_object_terms($pid, 'scos_topic', array('fields'=>'names'));
        $topic = (!is_wp_error($tp) && !empty($tp)) ? implode(', ', $tp) : '';

        // Use the real permalink (captures CPT rewrite bases + page hierarchy),
        // make it relative, then prepend the configured production domain so the
        // URL is correct AND production-mapped even when run against staging.
        $rel = '';
        $pl = get_permalink($pid);
        if ($pl && !is_wp_error($pl)) { $rel = wp_make_link_relative($pl); }
        if ($rel === '' && $slug) { $rel = '/' . $slug . '/'; }
        $production_url = $rel ? ($prod_base . $rel) : null;
        $gsc_url = $production_url
            ? ('https://search.google.com/search-console/performance/search-analytics?resource_id=' . $production_url)
            : null;
        $ga4_path = $rel ? $rel : null;

        $posts_out[] = array(
            'id' => (int) $pid,
            'title' => $post->post_title,
            'slug' => $slug,
            'post_type' => $pt,
            'post_date' => $post->post_date,
            'analysis_status' => $analysis_status,
            'word_count' => _nn(get_post_meta($pid, $prefix . 'word_count', true)),
            'h2_count' => _nn(get_post_meta($pid, $prefix . 'h2_count', true)),
            'image_count' => _nn(get_post_meta($pid, $prefix . 'image_count', true)),
            'reading_time' => _nn(get_post_meta($pid, $prefix . 'reading_time', true)),
            'internal_link_count' => _nn(get_post_meta($pid, $prefix . 'links_to_internal', true)),
            'external_link_count' => _nn(get_post_meta($pid, $prefix . 'links_to_external', true)),
            'last_analyzed' => _nn($last_analyzed),
            'scos_ca_intent' => $is_bw ? null : _nn(get_post_meta($pid, 'scos_ca_intent', true)),
            'scos_ca_purpose' => $is_bw ? null : _nn(get_post_meta($pid, 'scos_ca_purpose', true)),
            'scos_ca_maturity' => $is_bw ? null : _nn(get_post_meta($pid, 'scos_ca_maturity', true)),
            'scos_ca_index_status' => $is_bw ? null : _nn(get_post_meta($pid, 'scos_ca_index_status', true)),
            'scos_ca_optimization_progress' => $is_bw ? null : _nn(get_post_meta($pid, 'scos_ca_optimization_progress', true)),
            'scos_ca_next_step' => $is_bw ? null : _nn(get_post_meta($pid, 'scos_ca_next_step', true)),
            'scos_seo_title' => _nn(get_post_meta($pid, 'scos_seo_title', true)),
            'scos_seo_description' => _nn(get_post_meta($pid, 'scos_seo_description', true)),
            'scos_seo_robots' => _nn(get_post_meta($pid, 'scos_seo_robots', true)),
            'scos_seo_canonical' => _nn(get_post_meta($pid, 'scos_seo_canonical', true)),
            'scos_seo_breadcrumb_title' => _nn(get_post_meta($pid, 'scos_seo_breadcrumb_title', true)),
            'scos_seo_tldr' => _nn(get_post_meta($pid, 'scos_seo_tldr', true)),
            'cluster' => _nn($cluster),
            'topic' => _nn($topic),
            'production_url' => $production_url,
            'gsc_url' => $gsc_url,
            'ga4_path' => $ga4_path,
        );
    }
}

echo json_encode(array(
    'prefix' => $prefix,
    'found' => $found,
    'included' => $included,
    'excluded' => $excluded,
    'posts' => $posts_out,
), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PARTIAL_OUTPUT_ON_ERROR);
"""


# ---------------------------------------------------------------------------
# Step 0 — Validate siteurl
# ---------------------------------------------------------------------------

def _host_only(url: str) -> str:
    """Normalize a URL to bare host for identity comparison.

    The WP `siteurl` scheme (http vs https) can flip independently of which
    site you're actually pointed at, so we compare host only — scheme and
    trailing slash are irrelevant to identity and were a recurring tripwire.
    """
    return re.sub(r"^https?://", "", url.strip(), flags=re.IGNORECASE).rstrip("/").lower()


def validate_siteurl(client, wp_path: str, target_wp: str):
    siteurl = wp(client, wp_path, "option get siteurl")
    if _host_only(siteurl) != _host_only(target_wp):
        sys.exit(
            f"ERROR: siteurl host mismatch.\n"
            f"  WP reports: {siteurl}\n"
            f"  Expected:   {target_wp}\n"
            f"Check target-wordpress-domain in CLAUDE.md or --site argument."
        )


# ---------------------------------------------------------------------------
# Gather (single server-side pass)
# ---------------------------------------------------------------------------

def gather_inventory(client, wp_path: str, production_domain: str) -> dict:
    skip_list = ",".join(f"'{s}'" for s in SKIP_POST_TYPES)
    php = (
        _PHP_GATHER
        .replace("__PROD_BASE__", production_domain.rstrip("/"))
        .replace("__SKIP_LIST__", skip_list)
    )
    raw = wp_eval_stdin(client, wp_path, php)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(f"ERROR: could not parse gatherer output as JSON:\n{raw[:2000]}")


# ---------------------------------------------------------------------------
# Write output
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
        description="Gather WordPress content inventory via SSH/WP-CLI (single-pass)."
    )
    parser.add_argument("--site", required=True, help="Site slug, e.g. brighter-websites")
    args = parser.parse_args()
    site = args.site

    env = load_env()
    config = parse_claude_md(site)

    print(f"Connecting to {env['SSH_HOST']} ...")
    client = ssh_connect(env)
    validate_siteurl(client, env["WP_PATH"], config["target_wordpress_domain"])

    print("Gathering inventory (single server-side pass) ...")
    t0 = time.monotonic()
    result = gather_inventory(client, env["WP_PATH"], config["production_domain"])
    elapsed = time.monotonic() - t0
    client.close()

    posts = result.get("posts", [])
    prefix = result.get("prefix", "scos_ca_")
    found = result.get("found", [])
    included = result.get("included", [])
    excluded = result.get("excluded", [])

    total = len(posts)
    pending_count = sum(1 for p in posts if p.get("analysis_status") == "pending")
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
        f"Gather time: {elapsed:.1f}s (single pass)\n"
        f"File written: {out_path}\n"
        f"Next: run Task A (GSC/GA4) or Task B if traffic-signals.json already exists"
    )


if __name__ == "__main__":
    main()
