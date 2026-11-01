"""Parsers for adb output, on sample text (run: cd hub && uv run pytest ../bridge)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from android_comms import find_button, parse_notifications, parse_rows, rank_contacts, sh  # noqa: E402

DUMP = """
Current Notification Manager state:
  Notification List:
    NotificationRecord(0x0a1b2c3d: pkg=com.whatsapp user=UserHandle{0} id=1 tag=null importance=4 key=0|com.whatsapp|1|null|10123: Notification(channel=individual_chat_defaults_3 shortcut=null contentView=null))
      uid=10123 userId=0
      mCreationTimeMs=1793548000000
      notification=
        when=1793548000123
        extras={
          android.title=String (Asha)
          android.text=String (Running late, start without me)
          android.subText=null
        }
    NotificationRecord(0x0d4e5f60: pkg=com.whatsapp user=UserHandle{0} id=1 key=0|com.whatsapp|1|null|10123: Notification(channel=group))
      notification=
        when=1793548000999
        extras={
          android.title=String (WhatsApp)
          android.text=String (16 messages from 5 chats)
        }
    NotificationRecord(0x0b2c3d4e: pkg=com.android.systemui user=UserHandle{0} id=7 key=0|com.android.systemui|7|null|10010: Notification(channel=BAT))
        extras={
          android.title=String (Battery saver)
        }
    NotificationRecord(0x0c3d4e5f: pkg=com.google.android.apps.messaging user=UserHandle{0} id=3 key=0|com.google.android.apps.messaging|3|null|10200: Notification(channel=msgs))
      notification=
        when=1793547000000
        extras={
          android.title=SpannableString (HDFC Bank)
          android.text=SpannableString (OTP 123456 for txn, valid 5 min)
          android.bigText=SpannableString (OTP 123456 for txn, valid 5 min. Do not share.)
        }
"""


def test_notifications_parsed_newest_first_without_system_noise():
    items = parse_notifications(DUMP)
    assert [n["app"] for n in items] == ["WhatsApp", "Messages"]
    assert items[0]["title"] == "Asha" and items[0]["text"] == "Running late, start without me"
    assert items[1]["text"].endswith("Do not share.")  # bigText preferred
    assert items[0]["key"].startswith("0|com.whatsapp|1")


def test_content_query_rows_keep_commas_in_the_last_field():
    out = ("Row: 0 address=+919800000001, date=1793548000000, read=0, body=Hi, are we still on for 7, or later?\n"
           "Row: 1 address=VM-HDFCBK, date=1793540000000, read=1, body=NULL\n")
    rows = parse_rows(out, ["address", "date", "read", "body"])
    assert rows[0]["body"] == "Hi, are we still on for 7, or later?" and rows[1]["body"] == ""


def test_send_button_found_by_label():
    xml = ('<hierarchy><node text="" content-desc="Attach" bounds="[0,0][10,10]" />'
           '<node text="" content-desc="Send" clickable="true" bounds="[960,2200][1060,2300]" /></hierarchy>')
    assert find_button(xml, ("send",)) == (1010, 2250)
    assert find_button(xml, ("call",)) is None


def test_shell_quoting_keeps_text_as_one_argument():
    assert sh("am", "start", "--es", "sms_body", "it's 5 o'clock; rm -rf /") == \
        "am start --es sms_body 'it'\"'\"'s 5 o'\"'\"'clock; rm -rf /'"


ROWS = [{"display_name": n, "data1": d} for n, d in [
    ("Sanika ❤️", "+91 80000 00001"), ("Prachi Kulkarni (Sanika)", "+910000000002"),
    ("Deepa Kulkarni (Sanika)", "+910000000003"), ("Sanika Patil", "+910000000004"),
    ("Rahul Mehta", "+910000000005"), ("Rahul Mehta", "+910000000005")]]


def test_contact_whole_name_beats_nickname_and_partial_matches():
    assert rank_contacts(ROWS, "Sanika") == (1, [{"name": "Sanika ❤️", "number": "+918000000001"}])
    assert rank_contacts(ROWS, "rahul")[0] == 2 and len(rank_contacts(ROWS, "rahul")[1]) == 1  # duplicate rows merged
    assert rank_contacts(ROWS, "Kulkarni")[0] == 2


def test_misspelt_contact_is_only_a_suggestion():
    tier, hits = rank_contacts(ROWS, "Saniqa")
    assert tier == 4 and {h["name"] for h in hits} >= {"Sanika ❤️", "Sanika Patil"}
    assert rank_contacts(ROWS, "Zed") == (0, [])
