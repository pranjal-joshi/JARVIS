"""Tool cards: how people actually ask for each built-in tool, for picking the tools a small model is
offered. A request is matched against every example on its own (the best one counts), which finds
"is my phone about to die" far more reliably than one description per tool does. Tools in a family are
offered together, so the model chooses between siblings (set, list, cancel a timer) instead of
reaching for whichever one happened to score highest. Keys are internal tool names; tools without a
card (MCP servers) are matched on their description alone."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Card:
    examples: tuple[str, ...]
    family: str = ""
    not_for: str = ""  # appended to the description the model reads: "Not for: ..."


CARDS: dict[str, Card] = {
    # notes and memory
    "kb_search": Card(("what's my name", "where do I live", "what do you know about me", "when is my son's birthday",
                       "what did I say about the car", "do you remember my wife's name", "how do I take my coffee",
                       "what's the wifi password I noted"), "notes"),
    "kb_read_page": Card(("read my groceries page", "what's on my people page", "open my notes on the trip",
                          "show today's journal"), "notes"),
    "kb_tasks": Card(("what are my tasks", "what's left on my plate", "anything due this week", "what do I have to do",
                      "my to-do list", "anything coming up I should know about"), "notes"),
    "kb_remember": Card(("remember that I take tea without sugar", "note that my passport expires in 2031",
                         "keep in mind I prefer aisle seats", "don't forget my sister's birthday is 9 June",
                         "jot down that the plumber comes Friday"), "write-notes",
                        "a list or page the user names (use add_to_page)"),
    "kb_correct": Card(("no, my wife's name is Sanika, not Tanika", "that's wrong, fix it",
                        "you got my son's birthday wrong, it's 13 May", "correct my address, I live in Baner",
                        "update what you know about my car"), "write-notes",
                       "new facts (use remember)"),
    "kb_journal": Card(("add to my journal: finished the report", "log in my journal that I ran 5 km",
                        "journal entry: met Rohan for lunch"), "write-notes"),
    "kb_move_item": Card(("move the ESP Home tasks to my Jarvis Project page", "put everything about the car on the Car page",
                          "organize my notes about the trip into the Trip page", "move that note to Work",
                          "file the plumber note under Home"), "write-notes"),
    "kb_append_to_page": Card(("add bread to my groceries page", "put milk on the shopping list",
                               "add it to my groceries", "add Rohan's number to my people page"), "write-notes"),
    # time, maths, weather, timers
    "get_time": Card(("what time is it", "what's the date", "time in Tokyo", "what day is it today")),
    "calculate": Card(("what's 17 times 23", "15 percent of 2350", "how many days until Christmas",
                       "split 1840 between three people")),
    "weather": Card(("weather in Pune", "will it rain tomorrow", "should I carry an umbrella",
                     "is it cold outside", "how hot will it be today")),
    "set_timer": Card(("set a timer for 10 minutes", "wake me in 25 minutes", "remind me in 20 minutes to call mum",
                       "pasta timer for 8 minutes", "10 minutes for tea"), "timers"),
    "list_timers": Card(("how long left on the pasta", "what timers are running", "how much time is left"), "timers"),
    "cancel_timer": Card(("cancel the pasta timer", "never mind the timer", "stop the timer", "forget the tea timer"),
                         "timers"),
    "set_reminder": Card(("remind me every morning at 10 to follow up", "remind me tomorrow at 9 to call the bank",
                          "set a daily reminder at 8 pm", "remind me on my phone at 6 pm to leave",
                          "remind me every Monday to pay rent", "remind me at 10 AM tomorrow"), "timers",
                         "a countdown in minutes (use set_timer)"),
    "list_reminders": Card(("what reminders do I have", "show my reminders"), "timers"),
    "cancel_reminder": Card(("cancel the acrylic reminder", "stop reminding me to call the bank",
                             "delete my morning reminder"), "timers"),
    # skills (automations)
    "list_skills": Card(("what skills do you have", "list my skills", "which automations are on"), "skills"),
    "create_skill": Card(("make a skill that says hello world", "create a skill that tells me the weather every morning at 7",
                          "add an automation for when my phone connects", "new skill: summarise my notifications"), "skills",
                         "a one-off reminder (use set_reminder)"),
    "update_skill": Card(("change the morning skill to run at 8", "edit the hello world skill", "update that skill",
                          "add a step to the hello world skill", "make the skill also tell me the date"), "skills"),
    "delete_skill": Card(("delete the hello world skill", "remove that automation"), "skills"),
    "enable_skill": Card(("switch the morning skill off", "turn off that skill", "turn the hello world skill back on",
                          "pause that automation"), "skills"),
    "run_skill": Card(("run the hello world skill", "run that skill now", "try the skill"), "skills"),
    "dream": Card(("dream now", "go to sleep and dream", "consolidate your memory", "tidy up what you know about me",
                   "go through today's notes and keep what matters")),
    # laptop
    "win_info": Card(("how much battery does my laptop have", "how's the laptop holding up", "is my laptop charging",
                      "laptop memory use", "what window is open"), "laptop", "disk space or processes (use powershell_read)"),
    "win_ps_read": Card(("am I running out of space on C", "how much disk space is free", "what's using my CPU",
                         "how long has the laptop been up", "is the wifi connected", "what apps start at boot",
                         "find a file called report"), "laptop"),
    "win_media": Card(("pause the music", "skip this song", "next track", "turn the volume down on the laptop",
                       "mute the laptop", "play the music again"), "laptop",
                      "notifications or pop-ups (use send_notification)"),
    "win_open": Card(("open notepad", "open spotify", "open youtube.com on the laptop", "launch the calculator"),
                     "laptop"),
    "win_lock": Card(("lock my laptop", "lock the computer"), "laptop"),
    "win_screenshot": Card(("take a screenshot of my laptop", "grab a screenshot of the screen"), "laptop"),
    "fs_list": Card(("what's in my downloads folder", "list the files in my documents", "show the notes folder"),
                    "files"),
    "fs_read": Card(("read my packing list file", "open the readme on my laptop", "what does the todo.txt say"),
                    "files"),
    # phone
    "adb_devices": Card(("is my phone connected", "which phones are connected"), "phone"),
    "adb_info": Card(("is my phone about to die", "phone battery", "what's on my phone screen",
                      "how much charge on my phone"), "phone"),
    "adb_notifications": Card(("did anyone message me", "any new notifications", "any WhatsApp messages",
                               "did I get any mail", "what came in on my phone"), "phone-read",
                              "texts only (use read_texts) or calls (use recent_calls)"),
    "adb_sms_read": Card(("read me my latest texts", "any SMS from the bank", "read my text messages"), "phone-read"),
    "adb_call_log": Card(("who called me today", "any missed calls", "who did I call last"), "phone-read",
                         "messages or notifications (use phone_notifications)"),
    "adb_contacts": Card(("what's mum's number", "find Rohan in my contacts"), "phone-send"),
    "adb_sms_send": Card(("text mum I'm on my way", "let Sanika know I'll be home by 8", "send a message to Rohan",
                          "tell Chiku I'm running late"), "phone-send"),
    "adb_whatsapp_send": Card(("WhatsApp Rohan see you at 8", "send a whatsapp to mum"), "phone-send"),
    "adb_call": Card(("call mum", "ring Rohan", "phone my wife"), "phone-send"),
    "adb_open_app": Card(("open YouTube on my phone", "start maps on the phone"), "phone"),
    "adb_key": Card(("pause my phone", "phone volume up", "go home on the phone"), "phone"),
    "adb_screenshot": Card(("take a screenshot of my phone", "capture the phone screen"), "phone"),
    "adb_call_answer": Card(("answer the call", "pick up"), "phone-call"),
    "adb_call_end": Card(("hang up", "reject the call", "end the call"), "phone-call"),
    # notifications, web, experts
    "notify": Card(("ping my laptop saying stand up", "send a notification to my phone saying water the plants",
                    "notify me on my phone", "pop up a reminder on the laptop"), "",
                   "messages to other people (use send_text or send_whatsapp)"),
    "web_search": Card(("look up who won the F1 race", "latest news on the stock market", "current price of gold",
                        "search the web for the cricket score", "is the mall open now")),
    "fetch_page": Card(("read this web page", "what does this link say")),
    "ask_claude": Card(("ask Claude to explain public key cryptography", "ask Claude about this error")),
    "ask_opencode": Card(("ask opencode about this", "use opencode to explain")),
    # background jobs (long work by Claude Code on the laptop)
    "start_job": Card(("run a background job to research the best 2B models and write a summary",
                       "start a job to go through C:\\logs and find the errors",
                       "do this in the background: compare these three libraries", "run it as a job"), "jobs"),
    "job_status": Card(("how's the job going", "is the background job done yet", "what did the job find"), "jobs"),
    "cancel_job": Card(("stop the job", "cancel that background job"), "jobs"),
}


def card(name: str) -> Card | None:
    return CARDS.get(name)


def family_of(name: str) -> str:
    c = CARDS.get(name)
    return c.family if c else ""
