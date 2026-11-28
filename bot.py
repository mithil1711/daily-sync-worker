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
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
CLAN_TAG = os.environ["CLAN_TAG"].strip().upper()
if not CLAN_TAG.startswith("#"):
    CLAN_TAG = "#" + CLAN_TAG
ADMIN_ROLE_ID = os.environ.get("ADMIN_ROLE_ID", "").strip()

KICK_THRESHOLD = int(os.environ.get("KICK_THRESHOLD", 900))
SAFE_THRESHOLD = int(os.environ.get("SAFE_THRESHOLD", 1800))
ELDER_SAFE_THRESHOLD = int(os.environ.get("ELDER_SAFE_THRESHOLD", 2400))
BUFFER_MINUTES = 45
LOGIN_COOLDOWN_MINUTES = 14
EOW_FRESHNESS_HOURS = 48
WAR_PERIOD_TYPES = ("warDay", "colosseum")
WAR_BATTLE_TYPES = ("riverRacePvP", "riverRaceDuel", "riverRaceDuelColosseum", "boatBattle")
VALID_SOCIAL_STATUSES = {
    "none": "None",
    "discord": "Discord",
    "instagram": "Instagram",
    "both": "Both",
}

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


def api_get(endpoint, retries=2):
    url = f"{BASE_URL}{endpoint}"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 200:
                return resp.json()
            print(f"[API ERROR] {resp.status_code} on {endpoint} (attempt {attempt}/{retries}): {resp.text[:150]}")
            if resp.status_code not in (429, 500, 502, 503, 504):
                return None
        except (requests.RequestException, ValueError) as exc:
            print(f"[API NETWORK ERROR] on {endpoint} (attempt {attempt}/{retries}): {exc}")
        if attempt < retries:
            time.sleep(2 * attempt)
    return None


def encode_tag(tag):
    return urllib.parse.quote(tag.strip().upper())


def parse_cr_time(ts_str):
    """Parse Supercell UTC timestamp or saved ISO string and return an IST datetime."""
    if not ts_str:
        return None
    try:
        if "-" in ts_str:  # Already saved in ISO format
            return datetime.fromisoformat(ts_str).astimezone(IST)
        # Supercell API always returns UTC; convert immediately to IST
        dt_utc = datetime.strptime(ts_str[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(IST)
    except ValueError as exc:
        print(f"[TIME PARSE ERROR] Invalid timestamp '{ts_str}': {exc}")
        return None


def to_ist_iso(ts_str):
    dt = parse_cr_time(ts_str)
    return dt.isoformat(timespec="seconds") if dt else None


def fmt_ist(dt):
    return dt.astimezone(IST).strftime("%I:%M %p IST")


def fmt_ist_date(dt):
    return dt.astimezone(IST).strftime("%b %d, %I:%M %p IST")


def format_role(role):
    return {
        "leader": "Leader",
        "coLeader": "Co-Leader",
        "elder": "Elder",
        "member": "Member",
    }.get(role, role.capitalize())


def is_early_war_finished(war_state):
    """Return True if the current normal warDay week already finished early on Day 3."""
    if not war_state:
        return False
    early_sec = war_state.get("early_eow_section")
    curr_sec = war_state.get("section_index")
    return (
        early_sec is not None
        and curr_sec is not None
        and early_sec == curr_sec
        and war_state.get("period_type") == "warDay"
    )


def split_description(description, max_len=3800):
    if len(description) <= max_len:
        return [description]
    chunks = []
    current = []
    curr_len = 0
    for raw_line in description.split("\n"):
        sub_lines = [raw_line[i:i + max_len] for i in range(0, len(raw_line), max_len)] or [""]
        for line in sub_lines:
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


def send_discord_embed(title, description, color, url=None, ping=True):
    if not WEBHOOK_URL:
        print(f"[DISCORD ERROR] DISCORD_WEBHOOK_URL is not set. Skipped embed: {title}")
        return

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

        for attempt in range(1, 4):
            try:
                resp = requests.post(WEBHOOK_URL, json=payload, timeout=15)
                if resp.status_code in (200, 204):
                    break
                if resp.status_code == 429:
                    retry_after = 2.0
                    try:
                        retry_after = float(resp.json().get("retry_after", 2.0))
                    except Exception:
                        pass
                    time.sleep(min(retry_after, 15.0) + 0.5)
                    continue
                print(f"[DISCORD ERROR] {resp.status_code} (attempt {attempt}/3): {resp.text[:150]}")
                if resp.status_code < 500:
                    break
            except requests.RequestException as exc:
                print(f"[DISCORD NETWORK ERROR] (attempt {attempt}/3): {exc}")
            if attempt < 3:
                time.sleep(2 * attempt)

        time.sleep(1)


def send_clan_setting_reminder(setting_mode, reason, ping=True):
    """Ping @Admin to toggle clan settings between Invite Only and Open."""
    if setting_mode == "Invite Only":
        title = "🔒 Clan Setting Reminder: Switch to Invite Only"
        color = 0xE67E22
    else:
        title = "🔓 Clan Setting Reminder: Switch to Open"
        color = 0x2ECC71

    send_discord_embed(
        title=title,
        description=reason,
        color=color,
        ping=ping,
    )


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


def classify_score(score, kick_thresh=KICK_THRESHOLD, safe_thresh=SAFE_THRESHOLD):
    if score < kick_thresh:
        return "Red", "🔴 KICK", 0xE74C3C
    elif score < safe_thresh:
        return "Orange", "🟠 HOLD", 0xE67E22
    return "Green", "🟢 ACCEPT", 0x2ECC71


def purge_departed_before(departed, cutoff_dt):
    """Remove departed entries who left before cutoff_dt so pre-EOW leavers are re-vetted on rejoin."""
    if not cutoff_dt:
        return
    for tag in list(departed.keys()):
        left_dt = parse_cr_time(departed[tag].get("left_at"))
        if left_dt and left_dt < cutoff_dt:
            del departed[tag]


# ==========================================
# OPPONENT SPY CACHE & TODAY'S DECK CHECK
# ==========================================
def refresh_opponent_spy_cache(race_resp, war_state):
    """Cache 10-week opponent riverracelog players once per week + live current-week participants."""
    clans = race_resp.get("clans", [])
    opponents = {
        c["tag"]: c.get("name", c["tag"])
        for c in clans
        if c.get("tag") and c["tag"] != CLAN_TAG
    }
    if not opponents:
        return

    section_idx = race_resp.get("sectionIndex")
    week_key = f"{section_idx}|{'|'.join(sorted(opponents.keys()))}"
    cache = war_state.get("opponent_spy_cache") or {}

    # Refresh 10-week riverracelog cache once per war week (or if a previous fetch was incomplete)
    if cache.get("week_key") != week_key or cache.get("incomplete"):
        hist_players = {}
        incomplete = False
        for opp_tag, opp_name in opponents.items():
            rlog = api_get(f"/clans/{encode_tag(opp_tag)}/riverracelog?limit=10")
            if rlog is None:
                incomplete = True
                continue
            for weeks_ago, item in enumerate(rlog.get("items", []), start=1):
                for standing in item.get("standings", []):
                    if standing.get("clan", {}).get("tag") != opp_tag:
                        continue
                    for p in standing["clan"].get("participants", []):
                        if p.get("decksUsed", 0) > 0 or p.get("fame", 0) > 0:
                            p_tag = p["tag"]
                            existing = hist_players.get(p_tag)
                            if not existing or weeks_ago < existing["weeks_ago"]:
                                hist_players[p_tag] = {
                                    "clan_tag": opp_tag,
                                    "clan_name": opp_name,
                                    "weeks_ago": weeks_ago,
                                }
                    break

        cache = {
            "week_key": week_key,
            "incomplete": incomplete,
            "opponents": opponents,
            "players": hist_players,
            "current_week_players": {},
        }

    # Always refresh live current-week opponent participants from /currentriverrace (0 extra API calls)
    curr_week_players = {}
    for c in clans:
        opp_tag = c.get("tag")
        if not opp_tag or opp_tag == CLAN_TAG:
            continue
        opp_name = c.get("name", opp_tag)
        for p in c.get("participants", []):
            if p.get("decksUsed", 0) > 0 or p.get("fame", 0) > 0:
                curr_week_players[p["tag"]] = {
                    "clan_tag": opp_tag,
                    "clan_name": opp_name,
                }

    cache["opponents"] = opponents
    cache["current_week_players"] = curr_week_players
    war_state["opponent_spy_cache"] = cache


def check_today_war_decks_elsewhere(battles, war_state):
    """Return count of war decks used in another clan since today's Battle Day reset."""
    if not war_state or war_state.get("period_type") not in WAR_PERIOD_TYPES:
        return 0
    if is_early_war_finished(war_state):
        return 0
    day_start = parse_cr_time(war_state.get("day_start_ist"))
    if not day_start:
        return 0

    decks_used = 0
    for b in battles:
        if b.get("type") not in WAR_BATTLE_TYPES:
            continue
        bt = parse_cr_time(b.get("battleTime"))
        if not bt or bt < day_start:
            continue
        team_info = b.get("team", [{}])[0]
        clan_tag = (team_info.get("clan") or {}).get("tag")
        if clan_tag == CLAN_TAG:
            continue
        cards = team_info.get("cards") or []
        decks_used += max(1, len(cards) // 8)
    return min(4, decks_used)


def check_opponent_spy(tag, battles, war_state):
    """Check if player matches any active opponent via battlelog, current week, or 10-week cache."""
    cache = (war_state or {}).get("opponent_spy_cache") or {}
    opponents = cache.get("opponents") or {}
    if not opponents:
        return []

    alerts = []
    # 1. Recent battles in /battlelog wearing an opponent clan tag
    seen_battlelog_opps = set()
    for b in battles:
        clan_info = (b.get("team", [{}])[0].get("clan") or {})
        c_tag = clan_info.get("tag")
        if c_tag in opponents and c_tag not in seen_battlelog_opps:
            seen_battlelog_opps.add(c_tag)
            alerts.append(
                f"🚨 **SPY ALERT:** Recently battled for opponent **{opponents[c_tag]}** (`{c_tag}`) in battlelog!"
            )

    # 2. Live current-week opponent participants
    curr_match = (cache.get("current_week_players") or {}).get(tag)
    if curr_match and curr_match["clan_tag"] not in seen_battlelog_opps:
        alerts.append(
            f"🚨 **SPY ALERT:** Battled for opponent **{curr_match['clan_name']}** (`{curr_match['clan_tag']}`) in the **current war week**!"
        )

    # 3. Past 10 weeks riverracelog cache
    hist_match = (cache.get("players") or {}).get(tag)
    if hist_match:
        wk = hist_match["weeks_ago"]
        wk_label = "1 week ago" if wk == 1 else f"{wk} weeks ago"
        alerts.append(
            f"🚨 **SPY ALERT:** Played for opponent **{hist_match['clan_name']}** (`{hist_match['clan_tag']}`) **{wk_label}**!"
        )

    return alerts


# ==========================================
# 50/50 HOLD-LIST KICK SUGGESTION
# ==========================================
def build_kick_candidates(roster, socials, target_status):
    candidates = []
    for tag, info in roster.items():
        if info.get("status") != target_status:
            continue

        joined_dt = parse_cr_time(info.get("joined_at"))
        bonus, social_tier = get_social_priority(tag, socials)
        last_score = info.get("last_war_score") or 0
        eff_score = last_score + bonus
        joined_ts = joined_dt.timestamp() if joined_dt else 0

        candidates.append({
            "tag": tag,
            "name": info["name"],
            "role": format_role(info.get("role", "member")),
            "score": last_score,
            "bonus": bonus,
            "eff_score": eff_score,
            "social_tier": social_tier,
            "joined_ts": joined_ts,
            "badge": get_social_badge(tag, socials)[0],
        })

    candidates.sort(key=lambda x: (
        x["eff_score"],
        x["social_tier"],
        -x["joined_ts"],
    ))
    return candidates


def fmt_kick_score(m):
    if m["bonus"] > 0:
        return f"**{m['score']}** (+{m['bonus']}=**{m['eff_score']}**) medals"
    return f"**{m['score']} medals**"


def post_hold_kick_suggestion(roster, war_state, socials, trigger_name, trigger_action="joined", live_member_count=None):
    effective_count = live_member_count if live_member_count is not None else len(roster)
    if effective_count < 50:
        return

    # After a Day 3 early finish, Day 4 is treated as Training Day 0 (flag both Red and Hold/Orange)
    is_battle_day = war_state.get("period_type") in WAR_PERIOD_TYPES and not is_early_war_finished(war_state)
    curr_period_idx = war_state.get("period_index")
    war_day_num = max(1, min(4, (curr_period_idx % 7) - 2)) if (is_battle_day and curr_period_idx is not None) else 0

    # Gate 50/50 kick list behind Pending Review: ask admins to /score unvetted players first
    pending_members = [
        {
            "tag": tag,
            "name": info["name"],
            "role": format_role(info.get("role", "member")),
            "badge": get_social_badge(tag, socials)[0],
            "url": f"https://royaleapi.com/player/{tag.replace('#', '')}",
            "needs_vetting": bool(info.get("needs_vetting")),
        }
        for tag, info in roster.items()
        if info.get("status") == "Pending"
    ]
    if pending_members:
        pending_lines = []
        for m in pending_members:
            if m["needs_vetting"]:
                action_note = "⏳ *Auto-vetting queued for next poll*"
            else:
                action_note = f"[RoyaleAPI]({m['url']}) — `/score tag:{m['tag']} medals:<score>`"
            pending_lines.append(
                f"• **{m['name']}** ({m['role']}) {m['badge']} (`{m['tag']}`) — {action_note}"
            )
        desc = (
            f"Clan is at **50/50** after **{trigger_name}** {trigger_action}.\n"
            f"Classify the unvetted **Pending** player(s) first — once none remain, the 50/50 kick list will post automatically if still needed.\n\n"
            f"**⚪ Pending Review ({len(pending_members)}):**\n"
            + "\n".join(pending_lines)
        )
        send_discord_embed(
            title="⚪ 50/50 Reached — Pending Players Need /score First",
            description=desc,
            color=0x95A5A6,
        )
        return

    red_members = build_kick_candidates(roster, socials, "Red")

    # Battle Days (1-4, unless early finished): skip Hold/Orange kicks completely; only flag Red
    if is_battle_day:
        if not red_members:
            return
        red_lines = [
            f"{i}. **{m['name']}** ({m['role']}) {m['badge']} — {fmt_kick_score(m)} (`{m['tag']}`)"
            for i, m in enumerate(red_members, start=1)
        ]
        desc = (
            f"Clan is at **50/50** after **{trigger_name}** {trigger_action} (Battle Day {war_day_num} — Hold kicks skipped).\n"
            f"**Recommended Kick:** 👉 **{red_members[0]['name']}** (`{red_members[0]['tag']}`)\n\n"
            f"**🔴 Unkicked Red Members (Kick First):**\n" + "\n".join(red_lines)
        )
        send_discord_embed(
            title=f"⚖️ 50/50 Battle Day {war_day_num} — Unkicked Red Review",
            description=desc,
            color=0xE74C3C,
        )
        return

    # Training Days (and Day 4 after Day 3 early finish): Red (Kick First) + Orange (Hold List)
    hold_members = build_kick_candidates(roster, socials, "Orange")

    if not red_members and not hold_members:
        desc = f"Triggered after **{trigger_name}** {trigger_action}. No Red or eligible Orange (Hold) members to kick."
        send_discord_embed(
            title="✅ Clan at 50/50 — Hold List Empty",
            description=desc,
            color=0x2ECC71,
        )
        return

    sections = []
    if red_members:
        red_lines = [
            f"{i}. **{m['name']}** ({m['role']}) {m['badge']} — {fmt_kick_score(m)} (`{m['tag']}`)"
            for i, m in enumerate(red_members, start=1)
        ]
        sections.append("**🔴 Unkicked Red Members (Kick First):**\n" + "\n".join(red_lines))

    if hold_members:
        lines = [
            f"{i}. **{m['name']}** ({m['role']}) {m['badge']} — {fmt_kick_score(m)} (`{m['tag']}`)"
            for i, m in enumerate(hold_members, start=1)
        ]
        sections.append("**🟠 Hold List Priority:**\n" + "\n".join(lines))

    mode_label = "Lowest Medals + Social Priority (+50 IG / +100 DC) → Newest"
    top_pick = red_members[0] if red_members else hold_members[0]
    desc = (
        f"Clan is at **50/50** after **{trigger_name}** {trigger_action}.\n"
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
    if not war_end:
        return None, None, None, None
    war_start = war_end - timedelta(days=4)
    return war_start, war_end, latest["seasonId"], latest["sectionIndex"]


def vet_new_joiner(member, war_start, war_end, target_season, target_section, war_state=None, is_applicant=False):
    tag = member["tag"]
    name = member["name"]
    clean_tag = tag.replace("#", "")
    rapi_url = f"https://royaleapi.com/player/{clean_tag}"

    battles = api_get(f"/players/{encode_tag(tag)}/battlelog")
    if battles is None:
        return "Pending", None, True

    # Zero-extra-API-call safety checks: Today's Deck Check + Opponent Spy Check
    warning_lines = []
    decks_elsewhere = check_today_war_decks_elsewhere(battles, war_state)
    if decks_elsewhere > 0:
        warning_lines.append(
            f"⚠️ **War Deck Alert:** Already used **{decks_elsewhere}/4** war deck(s) in another clan today!"
        )
    spy_alerts = check_opponent_spy(tag, battles, war_state)
    warning_lines.extend(spy_alerts)
    warning_block = ("\n".join(warning_lines) + "\n\n") if warning_lines else ""

    war_window_clans = set()
    for b in battles:
        bt = parse_cr_time(b.get("battleTime"))
        if bt and war_start <= bt < war_end:
            clan_info = b.get("team", [{}])[0].get("clan")
            if clan_info and clan_info.get("tag"):
                war_window_clans.add(clan_info["tag"])

    if not war_window_clans:
        footer_note = (
            "*(Check RoyaleAPI before accepting.)*"
            if is_applicant
            else f"*(If you keep them, run `/score tag:{tag} medals:<score>` in Discord.)*"
        )
        desc = (
            f"{warning_block}"
            f"**Player:** {name} (`{tag}`)\n"
            f"**Status:** No clan battles found in last war window.\n"
            f"👉 **[Check RoyaleAPI CW2 History]({rapi_url})**\n"
            f"{footer_note}"
        )
        spy_prefix = "🚨 SPY | " if spy_alerts else ""
        send_discord_embed(
            title=f"{spy_prefix}🔎 {'Applicant' if is_applicant else 'New Joiner'}: {name} (Check RoyaleAPI)",
            description=desc,
            color=0xE74C3C if spy_alerts else 0x95A5A6,
            url=rapi_url,
        )
        return "Pending", None, False

    total_fame = 0
    total_decks = 0
    clan_notes = []

    for c_tag in war_window_clans:
        rlog = api_get(f"/clans/{encode_tag(c_tag)}/riverracelog?limit=3")
        if rlog is None:
            # Transient API failure on previous clan's war log: defer and retry instead of grading at 0
            return "Pending", None, True

        matched_war = None
        for item in rlog.get("items", []):
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
    if spy_alerts:
        color = 0xE74C3C

    desc = (
        f"{warning_block}"
        f"**Player:** {name} (`{tag}`) | **Verdict:** {verdict}\n"
        f"**Last War Cumulative Score:** **{total_fame} medals** ({total_decks} decks)\n"
        f"**Clans Checked:**\n• " + "\n• ".join(clan_notes)
    )
    spy_prefix = "🚨 SPY | " if spy_alerts else ""
    send_discord_embed(
        title=f"{spy_prefix}{'Applicant' if is_applicant else 'New Joiner'} Vetting: {name} — {verdict}",
        description=desc,
        color=color,
    )
    return status, total_fame, False


# ==========================================
# MODULE B: DAILY EOD & END-OF-WAR REPORTS
# ==========================================
MID_WAR_SAFE_LEAD = 2500
COLOSSEUM_SAFE_LEAD = 4000
SLACKER_MIN_LOGINS = 5


def get_mid_war_kick_status(war_state):
    """Evaluate mid-war kick status from saved daily clan standings."""
    standings = war_state.get("daily_standings") or []
    period_type = war_state.get("period_type")
    if not standings:
        return None

    ranked = sorted(standings, key=lambda x: x.get("points", 0), reverse=True)
    our_rank = None
    our_points = 0
    for idx, c in enumerate(ranked, start=1):
        if c.get("tag") == CLAN_TAG:
            our_rank = idx
            our_points = c.get("points", 0)
            break

    if our_rank is None:
        return None

    is_colosseum = period_type == "colosseum"
    safe_lead = COLOSSEUM_SAFE_LEAD if is_colosseum else MID_WAR_SAFE_LEAD

    if our_rank == 1:
        second_points = ranked[1].get("points", 0) if len(ranked) > 1 else 0
        lead = our_points - second_points
        if lead >= safe_lead:
            return {
                "active": False,
                "kick_count": 0,
                "banner": f"🛡️ **Mid-War Kicks: PAUSED** — Rank **#1** (+{lead:,} lead ≥ {safe_lead:,} safe buffer)",
            }
        kick_count = 4 if is_colosseum else 3
        label = "Colosseum" if is_colosseum else "Battle Day"
        return {
            "active": True,
            "kick_count": kick_count,
            "banner": f"🚨 **Mid-War Kicks: ACTIVE** — Rank **#1** (+{lead:,} lead < {safe_lead:,} {label} buffer) → **Kick top {kick_count}**",
        }

    first_points = ranked[0].get("points", 0)
    deficit = first_points - our_points
    return {
        "active": True,
        "kick_count": 4,
        "banner": f"🚨 **Mid-War Kicks: ACTIVE** — Rank **#{our_rank}** (-{deficit:,} behind 1st) → **Kick top 4**",
    }


def post_daily_eod_report(war_day_num, roster, war_state, socials, is_live=False, skip_mid_war_kicks=False):
    day_start = parse_cr_time(war_state.get("day_start_ist"))
    war_week_start = parse_cr_time(war_state.get("war_week_start_ist"))
    if not war_week_start and day_start:
        war_week_start = day_start - timedelta(days=war_day_num - 1)

    logged_in_offenders = []
    inactive_offenders = []
    kick_pool = []

    for tag, info in roster.items():
        decks = info.get("decks_today", 0)
        if decks >= 4:
            continue

        joined_dt = parse_cr_time(info.get("joined_at"))
        avail_days = war_day_num
        if war_week_start and joined_dt:
            avail_days = 0
            for d in range(1, war_day_num + 1):
                cutoff = war_week_start + timedelta(days=d) - timedelta(minutes=BUFFER_MINUTES)
                if joined_dt <= cutoff:
                    avail_days += 1
            # Joined after today's 45-minute cutoff: skip EOD shaming
            if avail_days == 0:
                continue

        logins = info.get("login_count", 0)
        last_seen = parse_cr_time(info.get("last_seen"))
        logged_in_today = logins > 0 or (last_seen and day_start and last_seen >= day_start)

        current_medals = info.get("fame", 0)
        elongated_medals = round(current_medals * (war_day_num / avail_days))
        if avail_days < war_day_num:
            now_str = f"{current_medals}→**{elongated_medals}** [{avail_days}/{war_day_num}d]"
        else:
            now_str = f"**{current_medals}**"

        raw_prev = info.get("last_war_score")
        prev_medals_sort = raw_prev if raw_prev is not None else 0
        prev_medals_str = str(raw_prev) if raw_prev is not None else "N/A"
        # Proxy prev score for unvetted joiners using current daily pace so they aren't unfairly targeted
        prev_eff = raw_prev if raw_prev is not None else round(elongated_medals * (4 / war_day_num))
        combined_score = prev_eff + elongated_medals

        entry = {
            "name": info["name"],
            "tag": tag,
            "role": info.get("role", "member"),
            "decks": decks,
            "skipped": 4 - decks,
            "logins": logins,
            "elongated_medals": elongated_medals,
            "now_str": now_str,
            "prev_medals_sort": prev_medals_sort,
            "prev_medals_str": prev_medals_str,
            "combined_score": combined_score,
            "last_seen_str": fmt_ist(last_seen) if last_seen else "Unknown",
            "last_seen_date_str": fmt_ist_date(last_seen) if last_seen else "Unknown",
            "badge": get_social_badge(tag, socials)[0],
        }

        if logged_in_today:
            logged_in_offenders.append(entry)
        else:
            inactive_offenders.append(entry)

        if entry["role"] != "leader":
            kick_pool.append(entry)

    logged_in_offenders.sort(key=lambda x: (
        x["decks"],
        -x["logins"],
        x["elongated_medals"],
        x["prev_medals_sort"],
    ))
    inactive_offenders.sort(key=lambda x: (
        x["decks"],
        x["elongated_medals"],
        x["prev_medals_sort"],
    ))

    if not logged_in_offenders and not inactive_offenders:
        desc = "Everyone has completed all 4/4 war battles! 🎉"
    else:
        sections = []

        # Show Mid-War Kick Status & 3-Tier Kick Order on Days 1–3 (Day 4 EOD is immediately followed by EOW)
        if not skip_mid_war_kicks and (war_day_num < 4 or is_live):
            kick_status = get_mid_war_kick_status(war_state)
            if kick_status:
                sections.append(kick_status["banner"])
                if kick_status["active"] and kick_pool:
                    tier1 = sorted(
                        [p for p in kick_pool if p["decks"] == 0 and p["logins"] >= SLACKER_MIN_LOGINS],
                        key=lambda x: (-x["logins"], x["combined_score"]),
                    )
                    tier2 = sorted(
                        [p for p in kick_pool if p["decks"] == 0 and p["logins"] < SLACKER_MIN_LOGINS],
                        key=lambda x: (x["combined_score"], -x["logins"]),
                    )
                    tier3 = sorted(
                        [p for p in kick_pool if p["decks"] > 0],
                        key=lambda x: (x["decks"], x["combined_score"], -x["logins"]),
                    )
                    ordered_kicks = (tier1 + tier2 + tier3)[:kick_status["kick_count"]]
                    kick_lines = []
                    for i, k in enumerate(ordered_kicks, start=1):
                        reason_tag = (
                            f"Slacker: 0/4 & {k['logins']} logins"
                            if (k["decks"] == 0 and k["logins"] >= SLACKER_MIN_LOGINS)
                            else f"{k['decks']}/4 | Prev+Now: **{k['combined_score']}**"
                        )
                        kick_lines.append(
                            f"{i}. **{k['name']}** {k['badge']} (`{k['tag']}`) — {reason_tag}"
                        )
                    sections.append(
                        f"**🎯 Suggested Mid-War Kicks (Top {len(ordered_kicks)}):**\n" + "\n".join(kick_lines)
                    )

        legend = "*Sorted by: Decks Used ↑ → Logins ↓ → Current War Medals (`Now`, projected for late joiners) ↑ → Last War Medals (`Prev`) ↑*"
        sections.append(legend)

        if logged_in_offenders:
            active_lines = [
                f"• **{o['name']}** {o['badge']} (`{o['tag']}`) — **{o['logins']} logins** (Last: {o['last_seen_str']}) | "
                f"Skipped **{o['skipped']}** ({o['decks']}/4) | Now: {o['now_str']} | Prev: **{o['prev_medals_str']}**"
                for o in logged_in_offenders
            ]
            sections.append(f"**👀 Logged In & Skipped ({len(logged_in_offenders)}):**\n" + "\n".join(active_lines))

        if inactive_offenders:
            inactive_lines = [
                f"• **{o['name']}** {o['badge']} (`{o['tag']}`) — Last seen: {o['last_seen_date_str']} | "
                f"Skipped **{o['skipped']}** ({o['decks']}/4) | Now: {o['now_str']} | Prev: **{o['prev_medals_str']}**"
                for o in inactive_offenders
            ]
            sections.append(f"**💤 Did Not Log In Today ({len(inactive_offenders)}):**\n" + "\n".join(inactive_lines))

        desc = "\n\n".join(sections)

    title_prefix = "⚡ Live Mid-Day" if is_live else "📊"
    send_discord_embed(
        title=f"{title_prefix} War Day {war_day_num} — Skipped War Battles",
        description=desc,
        color=0x3498DB,
        ping=not is_live and not skip_mid_war_kicks and war_day_num < 4,
    )

def _grade_and_post_eow(roster, socials, participants, war_end, total_war_days=4, deck_snapshot=None):
    war_start = war_end - timedelta(days=total_war_days)
    kick_thresh = round(KICK_THRESHOLD * total_war_days / 4)
    safe_thresh = round(SAFE_THRESHOLD * total_war_days / 4)
    elder_thresh = round(ELDER_SAFE_THRESHOLD * total_war_days / 4)

    evaluated = []
    skipped_late = []

    for tag, info in roster.items():
        badge, in_social = get_social_badge(tag, socials)
        role = info.get("role", "member")
        joined_at = parse_cr_time(info.get("joined_at"))

        if joined_at is None:
            avail_days = total_war_days
        else:
            avail_days = 0
            for d in range(1, total_war_days + 1):
                day_end = war_start + timedelta(days=d)
                cutoff = day_end - timedelta(minutes=BUFFER_MINUTES)
                if joined_at <= cutoff:
                    avail_days += 1

        # Joined after final War Day's 45-minute cutoff: skip grading & preserve vetted status
        if avail_days == 0:
            skipped_late.append(
                f"• **{info['name']}** ({format_role(role)}) {badge} (`{tag}`) — joined after Day {total_war_days} cutoff (kept `{info.get('status', 'Pending')}`)"
            )
            continue

        p_data = participants.get(tag, {"fame": 0, "decksUsed": 0})
        actual_medals = p_data.get("fame", 0)
        raw_api_decks = p_data.get("decksUsed", 0)

        snap = deck_snapshot.get(tag, info) if deck_snapshot is not None else info
        training_decks = snap.get("training_decks")
        if training_decks is not None:
            decks_used = max(0, raw_api_decks - training_decks)
        else:
            decks_used = min(snap.get("decks_used", 0), raw_api_decks)

        # Graded score is projected to total_war_days (3 or 4); normalized_4d keeps 50/50 Hold apples-to-apples
        graded_score = round(actual_medals * (total_war_days / avail_days))
        normalized_4d = round(actual_medals * (4 / avail_days))

        evaluated.append({
            "tag": tag,
            "name": info["name"],
            "role": role,
            "actual": actual_medals,
            "graded": graded_score,
            "normalized_4d": normalized_4d,
            "decks": decks_used,
            "avail_days": avail_days,
            "badge": badge,
            "in_social": in_social,
        })

    evaluated.sort(key=lambda x: (x["graded"], x["actual"], x["decks"]))

    kicks, demotes, holds, promotes, safes = [], [], [], [], []

    for e in evaluated:
        tag = e["tag"]
        role = e["role"]
        gr = e["graded"]
        score_str = f"{e['actual']}→**{gr}**" if e["avail_days"] < total_war_days else f"**{gr}**"
        day_str = f" [{e['avail_days']}/{total_war_days}d]" if e["avail_days"] < total_war_days else ""
        line = f"• **{e['name']}** ({format_role(role)}) {e['badge']} (`{tag}`) — {score_str} ({e['decks']} decks){day_str}"

        roster[tag]["last_war_score"] = e["normalized_4d"]
        roster[tag]["last_war_actual_score"] = e["actual"]
        roster[tag]["last_war_days"] = total_war_days

        if role == "leader":
            roster[tag]["status"] = "Green"
            safes.append(line)
            continue

        if role == "elder" and not e["in_social"]:
            if gr < kick_thresh:
                roster[tag]["status"] = "Red"
                kicks.append(line)
            elif gr >= elder_thresh or (e["decks"] >= e["avail_days"] * 4 and gr >= safe_thresh):
                roster[tag]["status"] = "Green"
                safes.append(line)
            else:
                roster[tag]["status"] = classify_score(gr, kick_thresh, safe_thresh)[0]
                demotes.append(line)
            continue

        # Co-Leader, Social Elder, and Member: strictly raw projected medals at EOW (no social bonus)
        if gr < kick_thresh:
            roster[tag]["status"] = "Red"
            kicks.append(line)
        elif gr < safe_thresh:
            roster[tag]["status"] = "Orange"
            holds.append(line)
        else:
            roster[tag]["status"] = "Green"
            if role == "member" and e["in_social"] and e["actual"] >= elder_thresh:
                promotes.append(line)
            else:
                safes.append(line)

    if total_war_days == 3:
        eow_prefix = "End of War (Day 3 Finish)"
        eow_note = (
            f"*Day 3 Early Finish — Scaled thresholds: Kick < {kick_thresh} | Hold < {safe_thresh} | Elder ≥ {elder_thresh}.*\n"
            f"*Format: `Actual→Projected [Days in War]` for late joiners.*\n\n"
        )
    else:
        eow_prefix = "End of War"
        eow_note = "*Format: `Actual→Projected [Days in War]` for late joiners.*\n\n"

    pinged = False

    def post_eow_section(title, lines, color, actionable=True):
        nonlocal pinged
        if not lines:
            return
        should_ping = actionable and not pinged
        send_discord_embed(title, eow_note + "\n".join(lines), color, ping=should_ping)
        if should_ping:
            pinged = True

    post_eow_section(f"🔴 {eow_prefix} — KICK ({len(kicks)})", kicks, 0xE74C3C)
    post_eow_section(f"🟡 {eow_prefix} — DEMOTE TO MEMBER ({len(demotes)})", demotes, 0xF1C40F)
    post_eow_section(f"🟠 {eow_prefix} — HOLD ({len(holds)})", holds, 0xE67E22)
    post_eow_section(f"⬆️ {eow_prefix} — PROMOTE TO ELDER ({len(promotes)})", promotes, 0x3498DB)
    post_eow_section(f"🟢 {eow_prefix} — SAFE ({len(safes)})", safes, 0x2ECC71, actionable=False)
    if skipped_late:
        send_discord_embed(
            f"⏳ {eow_prefix} — Joined Too Late to Grade ({len(skipped_late)})",
            "\n".join(skipped_late),
            0x95A5A6,
            ping=False,
        )

    return war_end


def post_end_of_war_verdict(roster, socials, now_ist, deck_snapshot=None):
    rlog = api_get(f"/clans/{encode_tag(CLAN_TAG)}/riverracelog?limit=1")
    if not rlog or not rlog.get("items"):
        print("[EOW DEFERRED] Failed to fetch riverracelog; will retry on next run.")
        return None

    latest = rlog["items"][0]
    war_end = parse_cr_time(latest.get("createdDate"))
    if not war_end or (now_ist - war_end) > timedelta(hours=EOW_FRESHNESS_HOURS):
        print(f"[EOW DEFERRED] Stale riverracelog (createdDate={latest.get('createdDate')}); will retry on next run.")
        return None

    participants = {}
    for standing in latest.get("standings", []):
        if standing["clan"]["tag"] == CLAN_TAG:
            for p in standing["clan"].get("participants", []):
                participants[p["tag"]] = p
            break

    return _grade_and_post_eow(
        roster,
        socials,
        participants,
        war_end,
        total_war_days=4,
        deck_snapshot=deck_snapshot,
    )


def post_early_end_of_war_verdict(roster, socials, race_resp, clan_finish_time, deck_snapshot=None):
    participants = {
        p["tag"]: p
        for p in race_resp.get("clan", {}).get("participants", [])
    }
    return _grade_and_post_eow(
        roster,
        socials,
        participants,
        clan_finish_time,
        total_war_days=3,
        deck_snapshot=deck_snapshot,
    )


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
        send_discord_embed(
            title="❌ Player Not Found in Roster",
            description=(
                f"Could not record score for `{tag}` because they are not currently in `roster.json`.\n"
                f"Check for a tag typo (e.g. `0` vs `O`), or wait for the next scheduled poll if they just joined."
            ),
            color=0xE74C3C,
            ping=False,
        )
        return

    status, verdict, color = classify_score(score)
    roster[tag]["last_war_score"] = score
    roster[tag]["status"] = status
    roster[tag].pop("needs_vetting", None)
    roster[tag].pop("vetting_queued_at", None)
    roster[tag].pop("vetting_delay_alerted", None)
    save_json(ROSTER_FILE, roster)

    name = roster[tag]["name"]
    send_discord_embed(
        title=f"📝 Manual Score Recorded: {name} — {verdict}",
        description=f"**Player:** {name} (`{tag}`)\n**Recorded Score:** **{score} medals**\n**Assigned Category:** {verdict}",
        color=color,
    )

    if status in ("Orange", "Green"):
        # Verify live clan size before firing 50/50 suggestion (fallback to local roster if API fails)
        members_resp = api_get(f"/clans/{encode_tag(CLAN_TAG)}/members")
        if members_resp and members_resp.get("items"):
            live_tags = {m["tag"] for m in members_resp["items"]}
            if len(live_tags) < 50:
                return
            live_roster = {t: info for t, info in roster.items() if t in live_tags}
            post_hold_kick_suggestion(
                live_roster,
                war_state,
                socials,
                name,
                trigger_action="was updated via `/score`",
                live_member_count=len(live_tags),
            )
        elif len(roster) >= 50:
            post_hold_kick_suggestion(
                roster,
                war_state,
                socials,
                name,
                trigger_action="was updated via `/score`",
                live_member_count=len(roster),
            )


# ==========================================
# SOCIAL STATUS UPDATE + ELDER ELIGIBILITY CHECK
# ==========================================
def handle_social_update(player_tag, social_status):
    tag = player_tag.strip().upper()
    if not tag.startswith("#"):
        tag = "#" + tag

    normalized_status = VALID_SOCIAL_STATUSES.get(social_status.strip().lower())
    if not normalized_status:
        send_discord_embed(
            title="❌ Invalid Social Status",
            description=(
                f"Invalid status `{social_status}` for `{tag}`.\n"
                f"Allowed values: `None`, `Discord`, `Instagram`, `Both`."
            ),
            color=0xE74C3C,
            ping=False,
        )
        return

    socials = load_json(SOCIALS_FILE, {})
    roster = load_json(ROSTER_FILE, {})

    socials[tag] = normalized_status
    save_json(SOCIALS_FILE, socials)

    badge, in_social = get_social_badge(tag, socials)
    info = roster.get(tag)

    if not info:
        send_discord_embed(
            title=f"✅ Social Status Updated: {tag} {badge}",
            description=(
                f"**Player:** `{tag}`\n"
                f"**Social Status:** **{normalized_status}** {badge}\n"
                f"*(Not currently in `roster.json`; status saved and will apply automatically once synced.)*"
            ),
            color=0x2ECC71,
            ping=False,
        )
        return

    name = info["name"]
    role = info.get("role", "member")
    actual_score = info.get("last_war_actual_score")
    last_war_days = info.get("last_war_days", 4)
    elder_thresh = round(ELDER_SAFE_THRESHOLD * last_war_days / 4)

    if in_social and role == "member" and actual_score is not None and actual_score >= elder_thresh:
        day_note = " (3-day finish)" if last_war_days == 3 else ""
        send_discord_embed(
            title=f"⬆️ Eligible for Elder Promotion: {name}",
            description=(
                f"**Player:** {name} ({format_role(role)}) {badge} (`{tag}`)\n"
                f"**Social Status Updated:** **{normalized_status}** {badge}\n"
                f"**Last Completed War (Actual):** **{actual_score} medals** (≥ {elder_thresh}{day_note})\n"
                f"👉 Meets all requirements — ready to promote to **Elder** in-game!"
            ),
            color=0x3498DB,
            ping=True,
        )
    else:
        bonus, _ = get_social_priority(tag, socials)
        bonus_note = f" (+{bonus} 50/50 Hold priority active)" if bonus > 0 else ""
        send_discord_embed(
            title=f"✅ Social Status Updated: {name} {badge}",
            description=(
                f"**Player:** {name} ({format_role(role)}) (`{tag}`)\n"
                f"**Social Status:** **{normalized_status}** {badge}{bonus_note}"
            ),
            color=0x2ECC71,
            ping=False,
        )


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
        send_discord_embed(
            title="⚠️ Applicant Vetting Unavailable",
            description=f"Temporary API error while fetching war window for `{tag}`. Please retry `/vet` in a minute.",
            color=0xE67E22,
            ping=False,
        )
        return

    war_state = load_json(WAR_STATE_FILE, {})

    # Reuse the exact same vetting logic (does not modify roster.json)
    _, _, retry_needed = vet_new_joiner(
        {"tag": player["tag"], "name": f"[Applicant] {player['name']}"},
        war_start,
        war_end,
        target_season,
        target_section,
        war_state=war_state,
        is_applicant=True,
    )
    if retry_needed:
        send_discord_embed(
            title=f"⚠️ Applicant Vetting Interrupted: {player['name']}",
            description=f"Temporary API error while fetching battle/war history for `{player['tag']}`. Please retry `/vet` in a minute.",
            color=0xE67E22,
            ping=False,
        )


# ==========================================
# MAIN EXECUTION LOOP
# ==========================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manual-tag", type=str, help="Player tag for manual RoyaleAPI score")
    parser.add_argument("--manual-score", type=int, help="Last war medals for manual entry")
    parser.add_argument("--social-tag", type=str, help="Player tag for social status update")
    parser.add_argument("--social-status", type=str, help="Social status: None, Discord, Instagram, Both")
    parser.add_argument("--vet-tag", type=str, help="Vet a Discord applicant before they join")
    parser.add_argument("--report", action="store_true", help="Send a live mid-day war report")
    args = parser.parse_args()

    if args.vet_tag is not None:
        handle_vet_applicant(args.vet_tag)
        return

    if args.social_tag is not None and args.social_status is not None:
        handle_social_update(args.social_tag, args.social_status)
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

    # Sync latest names & roles for existing members before Step 1 (EOD/EOW) runs
    for tag, m in current_members.items():
        if tag in roster:
            roster[tag]["name"] = m["name"]
            roster[tag]["role"] = m["role"]

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
    curr_section_idx = race_resp.get("sectionIndex")
    clan_finish_time = parse_cr_time(race_resp.get("clan", {}).get("finishTime"))
    curr_war_day_num = (
        max(1, min(4, (curr_period_idx % 7) - 2))
        if (curr_period_type in WAR_PERIOD_TYPES and curr_period_idx is not None)
        else 0
    )
    prev_period_idx = war_state.get("period_index")
    prev_period_type = war_state.get("period_type")
    eow_attempted_this_run = False
    clan_rapi_url = f"https://royaleapi.com/clan/{CLAN_TAG.replace('#', '')}/war/analytics"

    if prev_period_idx is not None and curr_period_idx != prev_period_idx:
        if prev_period_type in WAR_PERIOD_TYPES:
            war_day_num = max(1, min(4, (prev_period_idx % 7) - 2))
            early_finished_this_war = (
                war_state.get("early_eow_section") is not None
                and war_state.get("early_eow_section") == war_state.get("section_index")
                and prev_period_type == "warDay"
            )
            if not early_finished_this_war:
                is_day3_early_finish_now = (
                    curr_period_type == "warDay"
                    and curr_war_day_num == 4
                    and clan_finish_time is not None
                )
                post_daily_eod_report(
                    war_day_num,
                    roster,
                    war_state,
                    socials,
                    skip_mid_war_kicks=is_day3_early_finish_now,
                )

            if curr_period_type == "training":
                if not early_finished_this_war:
                    deck_snapshot = {
                        tag: {
                            "decks_used": info.get("decks_used", 0),
                            "training_decks": info.get("training_decks"),
                        }
                        for tag, info in roster.items()
                    }
                    eow_attempted_this_run = True
                    eow_war_end = post_end_of_war_verdict(roster, socials, now_ist, deck_snapshot)
                    if eow_war_end:
                        war_state.pop("pending_eow", None)
                        purge_departed_before(departed, eow_war_end)
                    else:
                        war_state["pending_eow"] = {
                            "created_at": now_ist_str,
                            "deck_snapshot": deck_snapshot,
                            "delay_alerted": False,
                        }
                    send_clan_setting_reminder(
                        "Open",
                        "War Week has ended! Switch clan settings back to **Open** for Training Day recruiting.",
                        ping=(eow_war_end is None),
                    )
                for store in (roster, departed):
                    for tag in store:
                        store[tag]["decks_prev_days"] = 0
                        store[tag]["decks_used"] = 0
                        store[tag]["training_decks"] = 0
            else:
                for store in (roster, departed):
                    for tag in store:
                        store[tag]["decks_prev_days"] = (
                            store[tag].get("decks_prev_days", 0)
                            + store[tag].get("decks_today", 0)
                        )
        elif curr_period_type in WAR_PERIOD_TYPES:
            # Transition from Training -> Battle Day 1
            war_state.pop("early_eow_section", None)
            if war_state.pop("pending_eow", None):
                send_discord_embed(
                    title="⚠️ End-of-War Verdict Expired",
                    description=(
                        f"Supercell's war log was never available before Battle Day 1 began, so last week's automated EOW report was skipped.\n"
                        f"👉 **[Check Clan War Analytics on RoyaleAPI]({clan_rapi_url})**"
                    ),
                    color=0xE67E22,
                    url=clan_rapi_url,
                )
            war_state["war_week_start_ist"] = now_ist_str
            for store in (roster, departed):
                for tag in store:
                    store[tag]["decks_prev_days"] = 0
                    store[tag]["decks_used"] = 0
            send_clan_setting_reminder(
                "Invite Only",
                "Battle Day 1 has started! Switch clan settings to **Invite Only** to block opponent minis and mid-day hoppers.",
            )

        for store in (roster, departed):
            for tag in store:
                store[tag]["login_count"] = 0
                store[tag]["last_login_counted_at"] = None
                store[tag]["decks_today"] = 0
        war_state["day_start_ist"] = now_ist_str

    # 1b. Retry deferred End-of-War verdict if previous riverracelog was stale or errored
    if not eow_attempted_this_run and war_state.get("pending_eow"):
        pending = war_state["pending_eow"]
        pending_dt = parse_cr_time(pending.get("created_at"))
        if curr_period_type != "training" or (pending_dt and (now_ist - pending_dt) > timedelta(hours=EOW_FRESHNESS_HOURS)):
            send_discord_embed(
                title="⚠️ End-of-War Verdict Expired (48h)",
                description=(
                    f"Supercell's `/riverracelog` remained unavailable or stale for over {EOW_FRESHNESS_HOURS} hours, so automated EOW grading was skipped.\n"
                    f"👉 **[Check Clan War Analytics on RoyaleAPI]({clan_rapi_url})**"
                ),
                color=0xE67E22,
                url=clan_rapi_url,
            )
            war_state.pop("pending_eow", None)
        else:
            eow_war_end = post_end_of_war_verdict(roster, socials, now_ist, pending.get("deck_snapshot"))
            if eow_war_end:
                war_state.pop("pending_eow", None)
                purge_departed_before(departed, eow_war_end)
            elif not pending.get("delay_alerted") and pending_dt and (now_ist - pending_dt) >= timedelta(hours=1):
                send_discord_embed(
                    title="⏳ End-of-War Verdict Delayed",
                    description=(
                        f"Waiting on Supercell's API to publish this week's `/riverracelog`. The bot will keep retrying automatically (up to {EOW_FRESHNESS_HOURS}h).\n"
                        f"👉 **[View Clan War Analytics on RoyaleAPI]({clan_rapi_url})**"
                    ),
                    color=0x95A5A6,
                    url=clan_rapi_url,
                    ping=False,
                )
                pending["delay_alerted"] = True

    war_state["period_index"] = curr_period_idx
    war_state["period_type"] = curr_period_type
    war_state["section_index"] = curr_section_idx
    if "day_start_ist" not in war_state:
        war_state["day_start_ist"] = now_ist_str

    # 1c. Automatic Day 3 Early Finish (normal warDay only — never colosseum)
    if (
        curr_period_type == "warDay"
        and curr_war_day_num == 4
        and clan_finish_time is not None
        and curr_section_idx is not None
        and war_state.get("early_eow_section") != curr_section_idx
    ):
        deck_snapshot = {
            tag: {
                "decks_used": info.get("decks_used", 0),
                "training_decks": info.get("training_decks"),
            }
            for tag, info in roster.items()
        }
        early_war_end = post_early_end_of_war_verdict(
            roster, socials, race_resp, clan_finish_time, deck_snapshot
        )
        war_state["early_eow_section"] = curr_section_idx
        war_state.pop("pending_eow", None)
        purge_departed_before(departed, early_war_end)
        send_clan_setting_reminder(
            "Open",
            "The clan crossed **10,000 Movement Points** on Battle Day 3! Switch clan settings back to **Open** for recruiting (Day 4 is now treated as Training Day 0).",
            ping=False,
        )

    # Refresh 10-week + current-week opponent spy cache before vetting newcomers
    refresh_opponent_spy_cache(race_resp, war_state)

    day_start = parse_cr_time(war_state.get("day_start_ist"))

    # 2. Map live war stats
    if curr_period_type in WAR_PERIOD_TYPES:
        war_state["daily_standings"] = [
            {
                "tag": c.get("tag"),
                "name": c.get("name", c.get("tag")),
                "points": c.get("fame", 0) if curr_period_type == "colosseum" else c.get("periodPoints", 0),
            }
            for c in race_resp.get("clans", [])
            if c.get("tag")
        ]
    decks_today_map = {}
    api_decks_used_map = {}
    fame_map = {}
    for p in race_resp.get("clan", {}).get("participants", []):
        api_decks_used_map[p["tag"]] = p.get("decksUsed", 0)
        if curr_period_type in WAR_PERIOD_TYPES:
            decks_today_map[p["tag"]] = p.get("decksUsedToday", 0)
            fame_map[p["tag"]] = p.get("fame", 0)

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
    eow_pending = bool(war_state.get("pending_eow"))

    def queue_vetting_retry(tag, member):
        roster[tag]["needs_vetting"] = True
        if "vetting_queued_at" not in roster[tag]:
            roster[tag]["vetting_queued_at"] = now_ist_str
        if not roster[tag].get("vetting_delay_alerted"):
            queued_dt = parse_cr_time(roster[tag].get("vetting_queued_at"))
            if queued_dt and (now_ist - queued_dt) >= timedelta(hours=1):
                rapi_url = f"https://royaleapi.com/player/{tag.replace('#', '')}"
                send_discord_embed(
                    title=f"⏳ Auto-Vetting Delayed: {member['name']}",
                    description=(
                        f"**Player:** {member['name']} (`{tag}`)\n"
                        f"Auto-vetting has been delayed over 1 hour due to Supercell API / pending EOW. The bot will keep retrying automatically.\n"
                        f"👉 **[Check RoyaleAPI CW2 History]({rapi_url})** or run `/score tag:{tag} medals:<score>` manually."
                    ),
                    color=0x95A5A6,
                    url=rapi_url,
                    ping=False,
                )
                roster[tag]["vetting_delay_alerted"] = True

    def try_vet_member(tag, member):
        nonlocal war_start, war_end, target_season, target_section
        if eow_pending:
            queue_vetting_retry(tag, member)
            return
        if war_start is None:
            war_start, war_end, target_season, target_section = get_last_completed_war_window()
        if war_start:
            status, score, retry_needed = vet_new_joiner(
                member,
                war_start,
                war_end,
                target_season,
                target_section,
                war_state=war_state,
            )
            if retry_needed:
                queue_vetting_retry(tag, member)
                return
            roster[tag]["status"] = status
            roster[tag]["last_war_score"] = score
            roster[tag].pop("needs_vetting", None)
            roster[tag].pop("vetting_queued_at", None)
            roster[tag].pop("vetting_delay_alerted", None)
            if status in ("Orange", "Green"):
                qualifying_joiners.append(member["name"])
        else:
            queue_vetting_retry(tag, member)

    for tag, m in current_members.items():
        curr_last_seen_ist = to_ist_iso(m.get("lastSeen"))
        today_decks = decks_today_map.get(tag, 0)
        api_decks_used = api_decks_used_map.get(tag, 0)

        if tag not in roster:
            if tag in departed:
                # Re-joiner within 7 days: restore original joined_at, status, and war stats
                restored = departed.pop(tag)
                prev_left_at = restored.pop("left_at", None)
                restored["name"] = m["name"]
                restored["role"] = m["role"]
                if curr_last_seen_ist:
                    restored["last_seen"] = curr_last_seen_ist
                prev_decks = restored.get("decks_prev_days", 0) if curr_period_type in WAR_PERIOD_TYPES else 0
                restored["decks_prev_days"] = prev_decks
                restored["decks_today"] = today_decks
                if curr_period_type == "training":
                    restored["training_decks"] = api_decks_used
                    restored["decks_used"] = 0
                elif curr_period_type in WAR_PERIOD_TYPES:
                    if curr_war_day_num == 1:
                        restored["training_decks"] = max(0, api_decks_used - today_decks)
                    t_decks = restored.get("training_decks")
                    if t_decks is not None:
                        restored["decks_used"] = max(0, api_decks_used - t_decks)
                    else:
                        restored["decks_used"] = prev_decks + today_decks
                else:
                    restored["decks_used"] = prev_decks + today_decks
                restored["fame"] = fame_map.get(tag, 0)
                roster[tag] = restored

                restored_status = restored.get("status")
                if restored_status == "Red":
                    badge = get_social_badge(tag, socials)[0]
                    left_dt = parse_cr_time(prev_left_at)
                    left_str = fmt_ist_date(left_dt) if left_dt else "Recently"
                    prev_score = restored.get("last_war_score")
                    score_str = f"**{prev_score} medals**" if prev_score is not None else "Unknown"
                    send_discord_embed(
                        title=f"🚨 Red / Kicked Player Rejoined: {m['name']}",
                        description=(
                            f"**Player:** {m['name']} ({format_role(m['role'])}) {badge} (`{tag}`)\n"
                            f"**Previous War Score:** {score_str}\n"
                            f"**Left Clan:** {left_str}\n"
                            f"*(Restored as `Red` from 7-day archive.)*"
                        ),
                        color=0xE74C3C,
                    )
                if restored.get("needs_vetting"):
                    try_vet_member(tag, m)
                elif restored_status == "Pending":
                    badge = get_social_badge(tag, socials)[0]
                    rapi_url = f"https://royaleapi.com/player/{tag.replace('#', '')}"
                    send_discord_embed(
                        title=f"🔎 Pending Player Rejoined: {m['name']}",
                        description=(
                            f"**Player:** {m['name']} ({format_role(m['role'])}) {badge} (`{tag}`)\n"
                            f"👉 **[Check RoyaleAPI CW2 History]({rapi_url})**\n"
                            f"*(Still unvetted — run `/score tag:{tag} medals:<score>` in Discord.)*"
                        ),
                        color=0x95A5A6,
                        url=rapi_url,
                    )
                elif restored_status in ("Orange", "Green"):
                    qualifying_joiners.append(m["name"])
            else:
                init_score = last_war_scores.get(tag) if first_run else None
                if first_run:
                    if m["role"] == "leader" or init_score is None:
                        init_status = "Green"
                    else:
                        init_status = classify_score(init_score)[0]
                else:
                    init_status = "Pending"

                init_training_decks = None
                if curr_period_type == "training":
                    init_training_decks = api_decks_used
                    init_decks_used = 0
                elif curr_period_type in WAR_PERIOD_TYPES:
                    if curr_war_day_num == 1:
                        init_training_decks = max(0, api_decks_used - today_decks)
                    elif not first_run:
                        init_training_decks = 0
                    init_decks_used = (
                        max(0, api_decks_used - init_training_decks)
                        if init_training_decks is not None
                        else today_decks
                    )
                else:
                    init_decks_used = today_decks

                roster[tag] = {
                    "name": m["name"],
                    "role": m["role"],
                    "joined_at": now_ist_str,
                    "last_seen": curr_last_seen_ist,
                    "login_count": 0,
                    "last_login_counted_at": None,
                    "decks_prev_days": 0,
                    "decks_today": today_decks,
                    "decks_used": init_decks_used,
                    "training_decks": init_training_decks,
                    "fame": fame_map.get(tag, 0),
                    "status": init_status,
                    "last_war_score": init_score,
                    "last_war_actual_score": init_score if first_run else None,
                    "last_war_days": 4,
                }
                if not first_run:
                    try_vet_member(tag, m)
        else:
            curr_dt = parse_cr_time(curr_last_seen_ist)
            prev_dt = parse_cr_time(roster[tag].get("last_seen"))
            if curr_dt and (prev_dt is None or curr_dt > prev_dt):
                # Only count logins that occurred at or after today's reset
                if not day_start or curr_dt >= day_start:
                    is_first_login_today = roster[tag].get("login_count", 0) == 0
                    counted_dt = parse_cr_time(roster[tag].get("last_login_counted_at")) or prev_dt
                    cooldown_elapsed = counted_dt is None or (curr_dt - counted_dt) >= timedelta(minutes=LOGIN_COOLDOWN_MINUTES)
                    if is_first_login_today or cooldown_elapsed:
                        roster[tag]["login_count"] = roster[tag].get("login_count", 0) + 1
                        roster[tag]["last_login_counted_at"] = curr_last_seen_ist
                    elif not roster[tag].get("last_login_counted_at") and roster[tag].get("last_seen"):
                        roster[tag]["last_login_counted_at"] = roster[tag]["last_seen"]
                roster[tag]["last_seen"] = curr_last_seen_ist
            prev_decks = roster[tag].get("decks_prev_days", 0) if curr_period_type in WAR_PERIOD_TYPES else 0
            roster[tag]["decks_prev_days"] = prev_decks
            roster[tag]["decks_today"] = today_decks
            if curr_period_type == "training":
                roster[tag]["training_decks"] = api_decks_used
                roster[tag]["decks_used"] = 0
            elif curr_period_type in WAR_PERIOD_TYPES:
                if curr_war_day_num == 1:
                    roster[tag]["training_decks"] = max(0, api_decks_used - today_decks)
                t_decks = roster[tag].get("training_decks")
                if t_decks is not None:
                    roster[tag]["decks_used"] = max(0, api_decks_used - t_decks)
                else:
                    roster[tag]["decks_used"] = prev_decks + today_decks
            else:
                roster[tag]["decks_used"] = prev_decks + today_decks
            roster[tag]["fame"] = fame_map.get(tag, 0)

            # Retry deferred vetting if a previous poll failed or waited on Monday EOW
            if roster[tag].get("needs_vetting"):
                try_vet_member(tag, m)

    # 5. Trigger 50/50 Hold-List Kick Suggestion if any Orange/Green joiner brought clan to 50/50
    if qualifying_joiners and len(roster) >= 50:
        post_hold_kick_suggestion(roster, war_state, socials, ", ".join(qualifying_joiners))

    # 6. On-demand live mid-day war report (/report)
    if args.report:
        if curr_period_type in WAR_PERIOD_TYPES:
            if is_early_war_finished(war_state):
                send_discord_embed(
                    title="🏁 War Already Finished (Day 3 Early Finish)",
                    description="The clan crossed **10,000 Movement Points** on Battle Day 3 and EOW grading has already been completed. Day 4 is treated as a rest/training day.",
                    color=0x2ECC71,
                    ping=False,
                )
            else:
                post_daily_eod_report(curr_war_day_num, roster, war_state, socials, is_live=True)
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
