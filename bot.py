import argparse
import json
import os
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

BASE_URL = "https://proxy.royaleapi.dev/v1"
HEADERS = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"}
IST = timezone(timedelta(hours=5, minutes=30))

DATA_DIR = "data"
ROSTER_FILE = os.path.join(DATA_DIR, "roster.json")
WAR_STATE_FILE = os.path.join(DATA_DIR, "war_state.json")
SOCIALS_FILE = os.path.join(DATA_DIR, "socials.json")

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
    if not ts_str:
        return None
    if "-" in ts_str:  # Already saved in IST/ISO format
        return datetime.fromisoformat(ts_str)
    return datetime.strptime(ts_str[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)


def to_ist_iso(ts_str):
    dt = parse_cr_time(ts_str)
    return dt.astimezone(IST).isoformat(timespec="seconds") if dt else None


def fmt_ist(dt):
    return dt.astimezone(IST).strftime("%I:%M %p IST")


def send_discord_embed(title, description, color, fields=None, url=None, ping=True):
    embed = {
        "title": title,
        "description": description[:4096],
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if url:
        embed["url"] = url
    if fields:
        embed["fields"] = fields[:25]

    # Always include top-level content for reliable mobile push previews
    if ping and ADMIN_ROLE_ID:
        content = f"<@&{ADMIN_ROLE_ID}> **{title}**"
        allowed_mentions = {"parse": ["roles"]}
    else:
        content = f"**{title}**"
        allowed_mentions = {"parse": []}

    payload = {
        "content": content,
        "embeds": [embed],
        "allowed_mentions": allowed_mentions,
    }
    resp = requests.post(WEBHOOK_URL, json=payload, timeout=15)
    if resp.status_code not in (200, 204):
        print(f"[DISCORD ERROR] {resp.status_code}: {resp.text}")

def get_social_badge(tag, socials):
    status = socials.get(tag, "None")
    mapping = {"Discord": "[DC]", "Instagram": "[IG]", "Both": "[DC+IG]", "None": "[None]"}
    return mapping.get(status, "[None]"), status != "None"


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

    hold_members = []
    for tag, info in roster.items():
        if info.get("status") == "Orange":
            hold_members.append({
                "tag": tag,
                "name": info["name"],
                "score": info.get("last_war_score") or 0,
                "decks_used": info.get("decks_used", 0),
                "logins": info.get("login_count", 0),
                "badge": get_social_badge(tag, socials)[0],
            })

    if not hold_members:
        send_discord_embed(
            title="✅ Clan at 50/50 — Hold List Empty",
            description=f"Triggered by **{trigger_name}**. No members are currently on the Orange (Hold) list—all 50 members are Green/Safe.",
            color=0x2ECC71,
        )
        return

    is_battle_day = war_state.get("period_type") == "warDay"

    if is_battle_day:
        # Battle Days: least decksUsed ASC, most logins DESC, lowest score ASC
        hold_members.sort(key=lambda x: (x["decks_used"], -x["logins"], x["score"]))
        mode_label = "Battle Days (Least Decks Used → Most Logins)"
        lines = [
            f"{i}. **{m['name']}** {m['badge']} — **{m['decks_used']} decks used** | **{m['logins']} logins** | Last war: {m['score']}"
            for i, m in enumerate(hold_members, start=1)
        ]
    else:
        # Training Days: lowest last_war_score ASC
        hold_members.sort(key=lambda x: x["score"])
        mode_label = "Training Days (Lowest Medals)"
        lines = [
            f"{i}. **{m['name']}** {m['badge']} — **{m['score']} medals**"
            for i, m in enumerate(hold_members, start=1)
        ]

    top_pick = hold_members[0]
    desc = (
        f"Clan reached **50/50** after **{trigger_name}** joined.\n"
        f"**Sorting Rule:** {mode_label}\n"
        f"**Recommended Kick (to maintain 49/50):** 👉 **{top_pick['name']}** (`{top_pick['tag']}`)\n\n"
        f"**Full Hold List Priority:**\n" + "\n".join(lines)
    )
    send_discord_embed(
        title="⚖️ 50/50 Roster Optimization — Suggested Kick from Hold List",
        description=desc,
        color=0xE67E22,
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


def count_war_decks_in_battle(battle):
    btype = battle.get("type", "")
    if "riverRace" not in btype and "boatBattle" not in btype:
        return 0
    if "Duel" in btype:
        rounds = battle.get("team", [{}])[0].get("rounds", [])
        return len(rounds) if rounds else 2
    return 1


def build_4day_breakdown(battles, war_start, war_end):
    if not battles:
        return "No battles available in Battle Log."

    battle_times = [parse_cr_time(b["battleTime"]) for b in battles if b.get("battleTime")]
    oldest_battle = min(battle_times)
    lines = []

    for day_num in range(4, 0, -1):
        d_start = war_start + timedelta(days=day_num - 1)
        d_end = war_start + timedelta(days=day_num)

        if oldest_battle >= d_end:
            lines.append(f"• **Day {day_num}:** Pushed off battle log")
            continue

        day_battles = [b for b in battles if d_start <= parse_cr_time(b["battleTime"]) < d_end]
        war_decks = sum(count_war_decks_in_battle(b) for b in day_battles)
        non_war = sum(1 for b in day_battles if count_war_decks_in_battle(b) == 0)

        if oldest_battle > d_start:
            lines.append(f"• **Day {day_num}:** Partial log ({war_decks}/4 war visible, earlier hours cut off)")
        elif war_decks >= 4:
            lines.append(f"• **Day {day_num}:** Completed ({war_decks}/4 war)")
        else:
            skipped = 4 - war_decks
            if non_war > 0:
                lines.append(f"• **Day {day_num}:** Active ({non_war} non-war) — skipped {skipped} war battles")
            else:
                lines.append(f"• **Day {day_num}:** Not active — skipped {skipped} war battles")

    return "\n".join(lines)


def vet_new_joiner(member, war_start, war_end, target_season, target_section):
    tag = member["tag"]
    name = member["name"]
    clean_tag = tag.replace("#", "")
    rapi_url = f"https://royaleapi.com/player/{clean_tag}/cw2"

    battles = api_get(f"/players/{encode_tag(tag)}/battlelog") or []

    war_window_clans = set()
    for b in battles:
        bt = parse_cr_time(b.get("battleTime"))
        if bt and war_start <= bt < war_end:
            clan_info = b.get("team", [{}])[0].get("clan")
            if clan_info and clan_info.get("tag"):
                war_window_clans.add(clan_info["tag"])

    breakdown = build_4day_breakdown(battles, war_start, war_end)

    if not war_window_clans:
        desc = (
            f"**Player:** {name} (`{tag}`)\n"
            f"**Status:** No clan battles found in last war window.\n"
            f"👉 **[Check RoyaleAPI CW2 History]({rapi_url})**\n"
            f"*(If you keep them, enter their score via GitHub Actions → **Manual Player Score**.)*\n\n"
            f"**War Day Breakdown (Last War):**\n{breakdown}"
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
        f"**Clans Checked:**\n• " + "\n• ".join(clan_notes) + "\n\n"
        f"**War Day Breakdown (Last War):**\n{breakdown}"
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
def post_daily_eod_report(war_day_num, roster, war_state, socials):
    day_start = datetime.fromisoformat(war_state["day_start_utc"])
    offenders = []

    for tag, info in roster.items():
        decks = info.get("decks_today", 0)
        logins = info.get("login_count", 0)
        last_seen = parse_cr_time(info.get("last_seen"))
        logged_in_today = logins > 0 or (last_seen and last_seen >= day_start)

        if logged_in_today and decks < 4:
            offenders.append({
                "name": info["name"],
                "tag": tag,
                "decks": decks,
                "skipped": 4 - decks,
                "logins": logins,
                "last_seen_str": fmt_ist(last_seen) if last_seen else "Unknown",
                "badge": get_social_badge(tag, socials)[0],
            })

    offenders.sort(key=lambda x: (x["decks"], -x["logins"]))

    if not offenders:
        desc = "Everyone who logged in completed all 4/4 war battles! 🎉"
    else:
        lines = [
            f"• **{o['name']}** {o['badge']} — **{o['logins']} logins** (Last: {o['last_seen_str']}) | Skipped **{o['skipped']}** ({o['decks']}/4)"
            for o in offenders
        ]
        desc = "\n".join(lines)

    send_discord_embed(
        title=f"📊 War Day {war_day_num} — Logged In & Skipped War",
        description=desc,
        color=0x3498DB,
    )


def post_end_of_war_verdict(roster, socials):
    war_start, war_end, season_id, section_idx = get_last_completed_war_window()
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
        joined_at = datetime.fromisoformat(info["joined_at"])

        avail_days = 0
        for d in range(1, 5):
            day_end = war_start + timedelta(days=d)
            cutoff = day_end - timedelta(minutes=BUFFER_MINUTES)
            if joined_at <= cutoff:
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

    if kicks:
        send_discord_embed(f"🔴 End of War — KICK ({len(kicks)})", "\n".join(kicks), 0xE74C3C)
    if demotes:
        send_discord_embed(f"🟡 End of War — DEMOTE TO MEMBER ({len(demotes)})", "\n".join(demotes), 0xF1C40F)
    if holds:
        send_discord_embed(f"🟠 End of War — HOLD ({len(holds)})", "\n".join(holds), 0xE67E22)
    if safes:
        send_discord_embed(f"🟢 End of War — SAFE ({len(safes)})", "\n".join(safes), 0x2ECC71)


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
    args = parser.parse_args()

    if args.vet_tag is not None:
        handle_vet_applicant(args.vet_tag)
        return

    if args.manual_tag is not None and args.manual_score is not None:
        handle_manual_score(args.manual_tag, args.manual_score)
        return

    now_ist = datetime.now(IST)
    roster = load_json(ROSTER_FILE, {})
    war_state = load_json(WAR_STATE_FILE, {})
    socials = load_json(SOCIALS_FILE, {})

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
        if prev_period_type == "warDay":
            war_day_num = max(1, min(4, (prev_period_idx % 7) - 2))
            post_daily_eod_report(war_day_num, roster, war_state, socials)
            if curr_period_type == "training":
                post_end_of_war_verdict(roster, socials)

        for tag in roster:
            roster[tag]["login_count"] = 0
        war_state["day_start_utc"] = now_ist.isoformat()

    war_state["period_index"] = curr_period_idx
    war_state["period_type"] = curr_period_type
    if "day_start_utc" not in war_state:
        war_state["day_start_utc"] = now_ist.isoformat()

    # 2. Map live decksUsedToday & weekly decksUsed
    decks_today_map = {}
    decks_used_map = {}
    for p in race_resp.get("clan", {}).get("participants", []):
        decks_today_map[p["tag"]] = p.get("decksUsedToday", 0)
        decks_used_map[p["tag"]] = p.get("decksUsed", 0)

    # 3. Remove players who left first (so roster count is accurate for 50/50 check)
    for tag in list(roster.keys()):
        if tag not in current_members:
            del roster[tag]

    # 4. Detect New Joiners & Update Roster / Login Counts
    war_start, war_end, target_season, target_section = (None, None, None, None)
    qualifying_joiners = []

    for tag, m in current_members.items():
        curr_last_seen_ist = to_ist_iso(m.get("lastSeen"))

        if tag not in roster:
            init_score = last_war_scores.get(tag) if first_run else None
            if first_run:
                if m["role"] in ("leader", "coLeader") or init_score is None:
                    init_status = "Green"
                else:
                    init_status = classify_score(init_score)[0]
            else:
                init_status = "Pending"

            roster[tag] = {
                "name": m["name"],
                "role": m["role"],
                "joined_at": now_ist.isoformat(timespec="seconds"),
                "last_seen": curr_last_seen_ist,
                "login_count": 0,
                "decks_today": decks_today_map.get(tag, 0),
                "decks_used": decks_used_map.get(tag, 0),
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
            roster[tag]["decks_today"] = decks_today_map.get(tag, 0)
            roster[tag]["decks_used"] = decks_used_map.get(tag, 0)
    # 5. Trigger 50/50 Hold-List Kick Suggestion if any Orange/Green joiner brought clan to 50/50
    if qualifying_joiners and len(roster) >= 50:
        post_hold_kick_suggestion(roster, war_state, socials, ", ".join(qualifying_joiners))

    save_json(ROSTER_FILE, roster)
    save_json(WAR_STATE_FILE, war_state)
    print(f"Run complete at {now_ist.isoformat()}. Tracked {len(roster)} members.")


if __name__ == "__main__":
    main()
