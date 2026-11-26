import argparse
import json
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
import requests

# ==========================================
# CONFIGURATION (Loaded from GitHub Secrets & Variables)
# ==========================================
API_TOKEN = os.environ["CR_API_TOKEN"]
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
CLAN_TAG = os.environ["CLAN_TAG"].strip().upper()
if not CLAN_TAG.startswith("#"):
    CLAN_TAG = "#" + CLAN_TAG
ADMIN_ROLE_ID = os.environ.get("ADMIN_ROLE_ID", "").strip()

KICK_THRESHOLD = int(os.environ.get("KICK_THRESHOLD", 900))
SAFE_THRESHOLD = int(os.environ.get("SAFE_THRESHOLD", 1800))
ELDER_SAFE_THRESHOLD = int(os.environ.get("ELDER_SAFE_THRESHOLD", 2400))
BUFFER_MINUTES = 45
WAR_PERIOD_TYPES = ("warDay", "colosseum")

BASE_URL = "https://proxy.royaleapi.dev/v1"
HEADERS = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"}
IST = timezone(timedelta(hours=5, minutes=30))

DATA_DIR = "data"
ROSTER_FILE = os.path.join(DATA_DIR, "roster.json")
WAR_STATE_FILE = os.path.join(DATA_DIR, "war_state.json")
SOCIALS_FILE = os.path.join(DATA_DIR, "socials.json")
DEPARTED_FILE = os.path.join(DATA_DIR, "departed.json")

os.makedirs(DATA_DIR, exist_ok=True)


# ==========================================
# HELPERS
# ==========================================
def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def api_get(endpoint):
    url = f"{BASE_URL}{endpoint}"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    if resp.status_code == 200:
        return resp.json()
    print(f"[API ERROR] {resp.status_code} on {endpoint}: {resp.text[:150]}")
    return None


def encode_tag(tag):
    return urllib.parse.quote(tag.strip().upper())


def parse_cr_time(ts_str):
    """Parse Supercell UTC timestamp or saved ISO string and return an IST datetime."""
    if not ts_str:
        return None
    if "-" in ts_str:  # Already saved in ISO format
        return datetime.fromisoformat(ts_str).astimezone(IST)
    # Supercell API always returns UTC; convert immediately to IST
    dt_utc = datetime.strptime(ts_str[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(IST)


def to_ist_iso(ts_str):
    dt = parse_cr_time(ts_str)
    return dt.isoformat(timespec="seconds") if dt else None


def fmt_ist(dt):
    return dt.astimezone(IST).strftime("%I:%M %p IST")


def split_description(description, max_len=3800):
    if len(description) <= max_len:
        return [description]
    chunks = []
    current = []
    curr_len = 0
    for line in description.split("\n"):
        line_len = len(line) + 1
        if current and curr_len + line_len > max_len:
            chunks.append("\n".join(current))
            current = [line]
            curr_len = line_len
        else:
            current.append(line)
            curr_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_discord_embed(title, description, color, fields=None, url=None, ping=True):
    chunks = split_description(description, max_len=3800)
    total = len(chunks)

    for idx, chunk in enumerate(chunks, start=1):
        part_title = f"{title} ({idx}/{total})" if total > 1 else title
        embed = {
            "title": part_title,
            "description": chunk[:4096],
            "color": color,
            "timestamp": datetime.now(IST).isoformat(timespec="seconds"),
        }
        if url and idx == 1:
            embed["url"] = url
        if fields and idx == 1:
            embed["fields"] = fields[:25]

        # Ping admins only on the first chunk to avoid duplicate notifications
        if ping and idx == 1 and ADMIN_ROLE_ID:
            content = f"<@&{ADMIN_ROLE_ID}> **{part_title}**"
            allowed_mentions = {"parse": ["roles"]}
        else:
            content = f"**{part_title}**"
            allowed_mentions = {"parse": []}

        payload = {
            "content": content,
            "embeds": [embed],
            "allowed_mentions": allowed_mentions,
        }

        resp = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        if resp.status_code == 429:
            retry_after = 2.0
            try:
                retry_after = float(resp.json().get("retry_after", 2.0))
            except Exception:
                pass
            time.sleep(retry_after + 0.5)
            resp = requests.post(WEBHOOK_URL, json=payload, timeout=15)

        if resp.status_code not in (200, 204):
            print(f"[DISCORD ERROR] {resp.status_code}: {resp.text}")

        time.sleep(1)


def get_social_badge(tag, socials):
    status = socials.get(tag, "None")
    mapping = {"Discord": "[DC]", "Instagram": "[IG]", "Both": "[DC+IG]", "None": "[None]"}
    return mapping.get(status, "[None]"), status != "None"


def get_social_priority(tag, socials):
    """Return (score_bonus, protection_tier): None=(0,0), IG=(50,1), DC=(100,2), Both=(100,3)."""
    status = socials.get(tag, "None")
    if status == "Both":
        return 100, 3
    if status == "Discord":
        return 100, 2
    if status == "Instagram":
        return 50, 1
    return 0, 0


def classify_score(score):
    if score < KICK_THRESHOLD:
        return "Red", "🔴 KICK", 0xE74C3C
    elif score < SAFE_THRESHOLD:
        return "Orange", "🟠 HOLD", 0xE67E22
    return "Green", "🟢 ACCEPT", 0x2ECC71


# ==========================================
# 50/50 HOLD-LIST KICK SUGGESTION
# ==========================================
def post_hold_kick_suggestion(roster, war_state, socials, trigger_name):
    if len(roster) < 50:
        return

    is_battle_day = war_state.get("period_type") in WAR_PERIOD_TYPES
    curr_period_idx = war_state.get("period_index")
    war_day_num = max(1, min(4, (curr_period_idx % 7) - 2)) if (is_battle_day and curr_period_idx is not None) else 0

    red_members = [
        {"tag": tag, "name": info["name"], "badge": get_social_badge(tag, socials)[0]}
        for tag, info in roster.items()
        if info.get("status") == "Red"
    ]

    pending_members = [
        {
            "tag": tag,
            "name": info["name"],
            "badge": get_social_badge(tag, socials)[0],
            "url": f"https://royaleapi.com/player/{tag.replace('#', '')}",
        }
        for tag, info in roster.items()
        if info.get("status") == "Pending"
    ]
    pending_section = ""
    if pending_members:
        pending_lines = [
            f"• **{m['name']}** {m['badge']} (`{m['tag']}`) — [RoyaleAPI]({m['url']})"
            for m in pending_members
        ]
        pending_section = (
            "**⚪ Pending Review (Unvetted):**\n"
            + "\n".join(pending_lines)
            + "\n*(Check RoyaleAPI and run `/score` in Discord to classify these players.)*"
        )

    # Battle Days (1-4): skip Hold/Orange kicks completely; only flag Red or Pending Review
    if is_battle_day:
        if not red_members and not pending_members:
            return
        sections = []
        if red_members:
            red_lines = [f"{i}. **{m['name']}** {m['badge']} (`{m['tag']}`)" for i, m in enumerate(red_members, start=1)]
            sections.append("**🔴 Unkicked Red Members (Kick First):**\n" + "\n".join(red_lines))
        if pending_section:
            sections.append(pending_section)
        desc = (
            f"Clan reached **50/50** after **{trigger_name}** joined (Battle Day {war_day_num} — Hold kicks skipped).\n\n"
            + "\n\n".join(sections)
        )
        send_discord_embed(
            title=f"⚖️ 50/50 Battle Day {war_day_num} — Red / Pending Review",
            description=desc,
            color=0xE74C3C if red_members else 0x95A5A6,
        )
        return

    # Training Days: Red + Hold/Orange + Pending Review
    hold_members = []
    for tag, info in roster.items():
        if info.get("status") != "Orange":
            continue

        joined_dt = parse_cr_time(info.get("joined_at"))
        bonus, social_tier = get_social_priority(tag, socials)
        last_score = info.get("last_war_score") or 0
        eff_score = last_score + bonus
        joined_ts = joined_dt.timestamp() if joined_dt else 0

        hold_members.append({
            "tag": tag,
            "name": info["name"],
            "score": last_score,
            "eff_score": eff_score,
            "social_tier": social_tier,
            "joined_ts": joined_ts,
            "badge": get_social_badge(tag, socials)[0],
        })

    if not red_members and not hold_members:
        desc = f"Triggered by **{trigger_name}**. No Red or eligible Orange (Hold) members to kick."
        if pending_section:
            desc += "\n\n" + pending_section
        send_discord_embed(
            title="✅ Clan at 50/50 — Hold List Empty",
            description=desc,
            color=0x2ECC71,
        )
        return

    sections = []
    if red_members:
        red_lines = [f"{i}. **{m['name']}** {m['badge']} (`{m['tag']}`)" for i, m in enumerate(red_members, start=1)]
        sections.append("**🔴 Unkicked Red Members (Kick First):**\n" + "\n".join(red_lines))

    if hold_members:
        hold_members.sort(key=lambda x: (
            x["eff_score"],
            x["social_tier"],
            -x["joined_ts"],
        ))
        mode_label = "Training Days (Lowest Medals + Social Priority → Newest)"
        lines = [
            f"{i}. **{m['name']}** {m['badge']} — **{m['score']} medals**"
            for i, m in enumerate(hold_members, start=1)
        ]
        sections.append("**🟠 Hold List Priority:**\n" + "\n".join(lines))
    else:
        mode_label = "Red Members Only"

    if pending_section:
        sections.append(pending_section)

    top_pick = red_members[0] if red_members else hold_members[0]
    desc = (
        f"Clan reached **50/50** after **{trigger_name}** joined.\n"
        f"**Sorting Rule:** {mode_label}\n"
        f"**Recommended Kick (to maintain 49/50):** 👉 **{top_pick['name']}** (`{top_pick['tag']}`)\n\n"
        + "\n\n".join(sections)
    )
    send_discord_embed(
        title="⚖️ 50/50 Roster Optimization — Suggested Kick",
        description=desc,
        color=0xE74C3C if red_members else 0xE67E22,
    )


# ==========================================
# MODULE A: NEW JOINER VETTING
# ==========================================
def get_last_completed_war_window():
    log = api_get(f"/clans/{encode_tag(CLAN_TAG)}/riverracelog?limit=1")
    if not log or not log.get("items"):
        return None, None, None, None
    latest = log["items"][0]
    war_end = parse_cr_time(latest["createdDate"])
    war_start = war_end - timedelta(days=4)
    return war_start, war_end, latest["seasonId"], latest["sectionIndex"]


def vet_new_joiner(member, war_start, war_end, target_season, target_section):
    tag = member["tag"]
    name = member["name"]
    clean_tag = tag.replace("#", "")
    rapi_url = f"https://royaleapi.com/player/{clean_tag}"

    battles = api_get(f"/players/{encode_tag(tag)}/battlelog") or []

    war_window_clans = set()
    for b in battles:
        bt = parse_cr_time(b.get("battleTime"))
        if bt and war_start <= bt < war_end:
            clan_info = b.get("team", [{}])[0].get("clan")
            if clan_info and clan_info.get("tag"):
                war_window_clans.add(clan_info["tag"])

    if not war_window_clans:
        desc = (
            f"**Player:** {name} (`{tag}`)\n"
            f"**Status:** No clan battles found in last war window.\n"
            f"👉 **[Check RoyaleAPI CW2 History]({rapi_url})**\n"
            f"*(If you keep them, run `/score tag:{tag} medals:<score>` in Discord.)*"
        )
        send_discord_embed(
            title=f"🔎 New Joiner: {name} (Check RoyaleAPI)",
            description=desc,
            color=0x95A5A6,
            url=rapi_url,
        )
        return "Pending", None

    total_fame = 0
    total_decks = 0
    clan_notes = []

    for c_tag in war_window_clans:
        rlog = api_get(f"/clans/{encode_tag(c_tag)}/riverracelog?limit=3")
        matched_war = None
        if rlog and rlog.get("items"):
            for item in rlog["items"]:
                if item["seasonId"] == target_season and item["sectionIndex"] == target_section:
                    matched_war = item
                    break

        if not matched_war:
            clan_notes.append(f"`{c_tag}` (did not participate in war)")
            continue

        found_in_standing = False
        for standing in matched_war.get("standings", []):
            if standing["clan"]["tag"] == c_tag:
                for p in standing["clan"].get("participants", []):
                    if p["tag"] == tag:
                        total_fame += p.get("fame", 0)
                        total_decks += p.get("decksUsed", 0)
                        clan_notes.append(f"{standing['clan']['name']} (`{c_tag}`): {p.get('fame', 0)} medals ({p.get('decksUsed', 0)} decks)")
                        found_in_standing = True
                        break
        if not found_in_standing:
            clan_notes.append(f"`{c_tag}`: 0 medals (left mid-war or 0 attacks)")

    status, verdict, color = classify_score(total_fame)

    desc = (
        f"**Player:** {name} (`{tag}`) | **Verdict:** {verdict}\n"
        f"**Last War Cumulative Score:** **{total_fame} medals** ({total_decks} decks)\n"
        f"**Clans Checked:**\n• " + "\n• ".join(clan_notes)
    )
    send_discord_embed(
        title=f"New Joiner Vetting: {name} — {verdict}",
        description=desc,
        color=color,
    )
    return status, total_fame


# ==========================================
# MODULE B: DAILY EOD & END-OF-WAR REPORTS
# ==========================================
def post_daily_eod_report(war_day_num, roster, war_state, socials, is_live=False):
    day_start_str = war_state.get("day_start_ist") or war_state.get("day_start_utc")
    day_start = parse_cr_time(day_start_str)
    offenders = []

    for tag, info in roster.items():
        decks = info.get("decks_today", 0)
        logins = info.get("login_count", 0)
        last_seen = parse_cr_time(info.get("last_seen"))
        logged_in_today = logins > 0 or (last_seen and day_start and last_seen >= day_start)

        if logged_in_today and decks < 4:
            current_medals = info.get("fame", 0)
            raw_prev = info.get("last_war_score")
            prev_medals_sort = raw_prev if raw_prev is not None else 0
            prev_medals_str = str(raw_prev) if raw_prev is not None else "N/A"

            offenders.append({
                "name": info["name"],
                "tag": tag,
                "decks": decks,
                "skipped": 4 - decks,
                "logins": logins,
                "current_medals": current_medals,
                "prev_medals_sort": prev_medals_sort,
                "prev_medals_str": prev_medals_str,
                "last_seen_str": fmt_ist(last_seen) if last_seen else "Unknown",
                "badge": get_social_badge(tag, socials)[0],
            })

    offenders.sort(key=lambda x: (
        x["decks"],
        -x["logins"],
        x["current_medals"],
        x["prev_medals_sort"],
    ))

    if not offenders:
        desc = "Everyone who logged in has completed all 4/4 war battles! 🎉"
    else:
        legend = "*Sorted by: Decks Used ↑ → Logins ↓ → Current War Medals (`Now`) ↑ → Last War Medals (`Prev`) ↑*\n\n"
        lines = [
            f"• **{o['name']}** {o['badge']} — **{o['logins']} logins** (Last: {o['last_seen_str']}) | "
            f"Skipped **{o['skipped']}** ({o['decks']}/4) | Now: **{o['current_medals']}** | Prev: **{o['prev_medals_str']}**"
            for o in offenders
        ]
        desc = legend + "\n".join(lines)

    title_prefix = "⚡ Live Mid-Day" if is_live else "📊"
    send_discord_embed(
        title=f"{title_prefix} War Day {war_day_num} — Logged In & Skipped War",
        description=desc,
        color=0x3498DB,
    )


def post_end_of_war_verdict(roster, socials):
    war_start, war_end, season_id, section_idx = get_last_completed_war_window()
    if not war_start:
        return

    rlog = api_get(f"/clans/{encode_tag(CLAN_TAG)}/riverracelog?limit=1")
    if not rlog or not rlog.get("items"):
        return

    participants = {}
    for standing in rlog["items"][0].get("standings", []):
        if standing["clan"]["tag"] == CLAN_TAG:
            for p in standing["clan"].get("participants", []):
                participants[p["tag"]] = p
            break

    evaluated = []
    for tag, info in roster.items():
        p_data = participants.get(tag, {"fame": 0, "decksUsed": 0})
        actual_medals = p_data.get("fame", 0)
        decks_used = p_data.get("decksUsed", 0)
        joined_at = parse_cr_time(info.get("joined_at"))

        avail_days = 0
        for d in range(1, 5):
            day_end = war_start + timedelta(days=d)
            cutoff = day_end - timedelta(minutes=BUFFER_MINUTES)
            if joined_at and joined_at <= cutoff:
                avail_days += 1

        avail_days = max(1, avail_days)
        elongated = round(actual_medals * (4 / avail_days))
        badge, in_social = get_social_badge(tag, socials)

        evaluated.append({
            "tag": tag,
            "name": info["name"],
            "role": info.get("role", "member"),
            "actual": actual_medals,
            "elongated": elongated,
            "decks": decks_used,
            "avail_days": avail_days,
            "badge": badge,
            "in_social": in_social,
        })

    evaluated.sort(key=lambda x: x["elongated"])
    bottom_5_tags = {e["tag"] for e in evaluated[:5]}

    kicks, demotes, holds, safes = [], [], [], []

    for e in evaluated:
        tag = e["tag"]
        role = e["role"]
        el = e["elongated"]
        score_str = f"{e['actual']}→**{el}**" if e["avail_days"] < 4 else f"**{el}**"
        day_str = f" [{e['avail_days']}/4d]" if e["avail_days"] < 4 else ""
        line = f"• **{e['name']}** ({role.capitalize()}) {e['badge']} — {score_str} ({e['decks']} decks){day_str}"

        roster[tag]["last_war_score"] = el

        if role in ("leader", "coLeader"):
            roster[tag]["status"] = "Green"
            safes.append(line)
            continue

        if role == "elder":
            if el <= KICK_THRESHOLD:
                roster[tag]["status"] = "Red"
                kicks.append(line)
            elif not e["in_social"]:
                if el > ELDER_SAFE_THRESHOLD or (e["decks"] >= 16 and el > SAFE_THRESHOLD):
                    roster[tag]["status"] = "Green"
                    safes.append(line)
                else:
                    roster[tag]["status"] = "Red"
                    demotes.append(line)
            else:
                if tag in bottom_5_tags and el > KICK_THRESHOLD:
                    roster[tag]["status"] = "Red"
                    demotes.append(line)
                elif el < SAFE_THRESHOLD:
                    roster[tag]["status"] = "Orange"
                    holds.append(line)
                else:
                    roster[tag]["status"] = "Green"
                    safes.append(line)
        else:
            if el < KICK_THRESHOLD:
                roster[tag]["status"] = "Red"
                kicks.append(line)
            elif el < SAFE_THRESHOLD:
                roster[tag]["status"] = "Orange"
                holds.append(line)
            else:
                roster[tag]["status"] = "Green"
                safes.append(line)

    eow_note = "*Format: `Actual→Projected [Days in War]` for late joiners.*\n\n"
    if kicks:
        send_discord_embed(f"🔴 End of War — KICK ({len(kicks)})", eow_note + "\n".join(kicks), 0xE74C3C)
    if demotes:
        send_discord_embed(f"🟡 End of War — DEMOTE TO MEMBER ({len(demotes)})", eow_note + "\n".join(demotes), 0xF1C40F)
    if holds:
        send_discord_embed(f"🟠 End of War — HOLD ({len(holds)})", eow_note + "\n".join(holds), 0xE67E22)
    if safes:
        send_discord_embed(f"🟢 End of War — SAFE ({len(safes)})", eow_note + "\n".join(safes), 0x2ECC71)


# ==========================================
# MANUAL SCORE ENTRY (FOR ROYALEAPI FALLBACK)
# ==========================================
def handle_manual_score(player_tag, score):
    tag = player_tag.strip().upper()
    if not tag.startswith("#"):
        tag = "#" + tag

    roster = load_json(ROSTER_FILE, {})
    war_state = load_json(WAR_STATE_FILE, {})
    socials = load_json(SOCIALS_FILE, {})

    if tag not in roster:
        print(f"[ERROR] Player {tag} is not currently in roster.json.")
        return

    status, verdict, color = classify_score(score)
    roster[tag]["last_war_score"] = score
    roster[tag]["status"] = status
    save_json(ROSTER_FILE, roster)

    name = roster[tag]["name"]
    send_discord_embed(
        title=f"📝 Manual Score Recorded: {name} — {verdict}",
        description=f"**Player:** {name} (`{tag}`)\n**Recorded Score:** **{score} medals**\n**Assigned Category:** {verdict}",
        color=color,
    )

    if status in ("Orange", "Green") and len(roster) >= 50:
        post_hold_kick_suggestion(roster, war_state, socials, name)


# ==========================================
# ON-DEMAND APPLICANT VETTING (PRE-JOIN)
# ==========================================
def handle_vet_applicant(player_tag):
    tag = player_tag.strip().upper()
    if not tag.startswith("#"):
        tag = "#" + tag

    player = api_get(f"/players/{encode_tag(tag)}")
    if not player:
        send_discord_embed(
            title="❌ Applicant Not Found",
            description=f"Could not find a Clash Royale player with tag `{tag}`.",
            color=0xE74C3C,
        )
        return

    war_start, war_end, target_season, target_section = get_last_completed_war_window()
    if not war_start:
        print("Failed to fetch clan riverracelog for war window.")
        return

    # Reuse the exact same vetting logic (does not modify roster.json)
    vet_new_joiner(
        {"tag": player["tag"], "name": f"[Applicant] {player['name']}"},
        war_start,
        war_end,
        target_season,
        target_section,
    )


# ==========================================
# MAIN EXECUTION LOOP
# ==========================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manual-tag", type=str, help="Player tag for manual RoyaleAPI score")
    parser.add_argument("--manual-score", type=int, help="Last war medals for manual entry")
    parser.add_argument("--vet-tag", type=str, help="Vet a Discord applicant before they join")
    parser.add_argument("--report", action="store_true", help="Send a live mid-day war report")
    args = parser.parse_args()

    if args.vet_tag is not None:
        handle_vet_applicant(args.vet_tag)
        return

    if args.manual_tag is not None and args.manual_score is not None:
        handle_manual_score(args.manual_tag, args.manual_score)
        return

    now_ist = datetime.now(IST)
    now_ist_str = now_ist.isoformat(timespec="seconds")
    roster = load_json(ROSTER_FILE, {})
    war_state = load_json(WAR_STATE_FILE, {})
    socials = load_json(SOCIALS_FILE, {})
    departed = load_json(DEPARTED_FILE, {})

    members_resp = api_get(f"/clans/{encode_tag(CLAN_TAG)}/members")
    race_resp = api_get(f"/clans/{encode_tag(CLAN_TAG)}/currentriverrace")
    if not members_resp or not race_resp:
        print("Failed to fetch clan data. Exiting.")
        return

    current_members = {m["tag"]: m for m in members_resp.get("items", [])}
    first_run = len(roster) == 0

    last_war_scores = {}
    if first_run:
        rlog = api_get(f"/clans/{encode_tag(CLAN_TAG)}/riverracelog?limit=1")
        if rlog and rlog.get("items"):
            for standing in rlog["items"][0].get("standings", []):
                if standing["clan"]["tag"] == CLAN_TAG:
                    for p in standing["clan"].get("participants", []):
                        last_war_scores[p["tag"]] = p.get("fame", 0)
                    break

    # 1. Check for War Day / War End Transition
    curr_period_idx = race_resp.get("periodIndex")
    curr_period_type = race_resp.get("periodType")
    prev_period_idx = war_state.get("period_index")
    prev_period_type = war_state.get("period_type")

    if prev_period_idx is not None and curr_period_idx != prev_period_idx:
        if prev_period_type in WAR_PERIOD_TYPES:
            war_day_num = max(1, min(4, (prev_period_idx % 7) - 2))
            post_daily_eod_report(war_day_num, roster, war_state, socials)
            if curr_period_type == "training":
                post_end_of_war_verdict(roster, socials)
                for store in (roster, departed):
                    for tag in store:
                        store[tag]["decks_prev_days"] = 0
                        store[tag]["prev_day_decks"] = 0
                        store[tag]["prev_day_logins"] = 0
            else:
                for store in (roster, departed):
                    for tag in store:
                        store[tag]["prev_day_decks"] = store[tag].get("decks_today", 0)
                        store[tag]["prev_day_logins"] = store[tag].get("login_count", 0)
                        store[tag]["decks_prev_days"] = (
                            store[tag].get("decks_prev_days", 0)
                            + store[tag].get("decks_today", 0)
                        )
        elif curr_period_type in WAR_PERIOD_TYPES:
            # Transition from Training -> Battle Day 1
            war_state["war_week_start_ist"] = now_ist_str
            for store in (roster, departed):
                for tag in store:
                    store[tag]["decks_prev_days"] = 0
                    store[tag]["prev_day_decks"] = 0
                    store[tag]["prev_day_logins"] = 0

        for store in (roster, departed):
            for tag in store:
                store[tag]["login_count"] = 0
        war_state["day_start_ist"] = now_ist_str

    war_state["period_index"] = curr_period_idx
    war_state["period_type"] = curr_period_type
    if "day_start_ist" not in war_state:
        war_state["day_start_ist"] = war_state.pop("day_start_utc", now_ist_str)
    else:
        war_state.pop("day_start_utc", None)

    # 2. Map live war stats (War Days / Colosseum only)
    decks_today_map = {}
    fame_map = {}
    boat_attacks_map = {}
    if curr_period_type in WAR_PERIOD_TYPES:
        for p in race_resp.get("clan", {}).get("participants", []):
            decks_today_map[p["tag"]] = p.get("decksUsedToday", 0)
            fame_map[p["tag"]] = p.get("fame", 0)
            boat_attacks_map[p["tag"]] = p.get("boatAttacks", 0)

    # 3. Archive players who left (7-day grace period) and remove from active roster
    for tag in list(roster.keys()):
        if tag not in current_members:
            archived = roster.pop(tag)
            archived["left_at"] = now_ist_str
            departed[tag] = archived

    for tag in list(departed.keys()):
        left_dt = parse_cr_time(departed[tag].get("left_at"))
        if not left_dt or (now_ist - left_dt) > timedelta(days=7):
            del departed[tag]

    # 4. Detect New Joiners / Re-joiners & Update Roster / Login Counts
    war_start, war_end, target_season, target_section = (None, None, None, None)
    qualifying_joiners = []

    for tag, m in current_members.items():
        curr_last_seen_ist = to_ist_iso(m.get("lastSeen"))

        if tag not in roster:
            if tag in departed:
                # Re-joiner within 7 days: restore original joined_at, status, and war stats
                restored = departed.pop(tag)
                restored.pop("left_at", None)
                restored["name"] = m["name"]
                restored["role"] = m["role"]
                if curr_last_seen_ist:
                    restored["last_seen"] = curr_last_seen_ist
                today_decks = decks_today_map.get(tag, 0)
                prev_decks = restored.get("decks_prev_days", 0) if curr_period_type in WAR_PERIOD_TYPES else 0
                restored["decks_prev_days"] = prev_decks
                restored["decks_today"] = today_decks
                restored["decks_used"] = prev_decks + today_decks
                restored["fame"] = fame_map.get(tag, 0)
                restored["boat_attacks"] = boat_attacks_map.get(tag, 0)
                roster[tag] = restored
                if restored.get("status") in ("Orange", "Green"):
                    qualifying_joiners.append(m["name"])
            else:
                init_score = last_war_scores.get(tag) if first_run else None
                if first_run:
                    if m["role"] in ("leader", "coLeader") or init_score is None:
                        init_status = "Green"
                    else:
                        init_status = classify_score(init_score)[0]
                else:
                    init_status = "Pending"

                today_decks = decks_today_map.get(tag, 0)
                roster[tag] = {
                    "name": m["name"],
                    "role": m["role"],
                    "joined_at": now_ist_str,
                    "last_seen": curr_last_seen_ist,
                    "login_count": 0,
                    "decks_prev_days": 0,
                    "prev_day_decks": 0,
                    "prev_day_logins": 0,
                    "decks_today": today_decks,
                    "decks_used": today_decks,
                    "fame": fame_map.get(tag, 0),
                    "boat_attacks": boat_attacks_map.get(tag, 0),
                    "status": init_status,
                    "last_war_score": init_score,
                }
                if not first_run:
                    if war_start is None:
                        war_start, war_end, target_season, target_section = get_last_completed_war_window()
                    if war_start:
                        status, score = vet_new_joiner(m, war_start, war_end, target_season, target_section)
                        roster[tag]["status"] = status
                        roster[tag]["last_war_score"] = score
                        if status in ("Orange", "Green"):
                            qualifying_joiners.append(m["name"])
        else:
            prev_last_seen = roster[tag].get("last_seen")
            if curr_last_seen_ist and curr_last_seen_ist != prev_last_seen:
                roster[tag]["login_count"] = roster[tag].get("login_count", 0) + 1
                roster[tag]["last_seen"] = curr_last_seen_ist
            roster[tag]["name"] = m["name"]
            roster[tag]["role"] = m["role"]
            today_decks = decks_today_map.get(tag, 0)
            prev_decks = roster[tag].get("decks_prev_days", 0) if curr_period_type in WAR_PERIOD_TYPES else 0
            roster[tag]["decks_prev_days"] = prev_decks
            roster[tag]["decks_today"] = today_decks
            roster[tag]["decks_used"] = prev_decks + today_decks
            roster[tag]["fame"] = fame_map.get(tag, 0)
            roster[tag]["boat_attacks"] = boat_attacks_map.get(tag, 0)

    # 5. Trigger 50/50 Hold-List Kick Suggestion if any Orange/Green joiner brought clan to 50/50
    if qualifying_joiners and len(roster) >= 50:
        post_hold_kick_suggestion(roster, war_state, socials, ", ".join(qualifying_joiners))

    # 6. On-demand live mid-day war report (/report)
    if args.report:
        if curr_period_type in WAR_PERIOD_TYPES:
            war_day_num = max(1, min(4, (curr_period_idx % 7) - 2))
            post_daily_eod_report(war_day_num, roster, war_state, socials, is_live=True)
        else:
            send_discord_embed(
                title="ℹ️ Live War Report Unavailable",
                description="The clan is currently in **Training Days**. Live war reports are only available during Battle Days.",
                color=0x95A5A6,
                ping=False,
            )

    save_json(ROSTER_FILE, roster)
    save_json(WAR_STATE_FILE, war_state)
    save_json(DEPARTED_FILE, departed)
    print(f"Run complete at {now_ist_str}. Tracked {len(roster)} members.")


if __name__ == "__main__":
    main()
