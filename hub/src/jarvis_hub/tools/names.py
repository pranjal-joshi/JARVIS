"""Tool names as the model sees them. Small models pick a tool mostly by its name, so these follow how
people talk ("send_text", not "adb_sms_send"). Everything else (audit log, events, the HUD, approvals,
settings) keeps the internal name. Tools not listed here (MCP servers, get_time...) keep their own."""

MODEL_NAMES = {
    "kb_search": "search_notes",
    "kb_read_page": "read_page",
    "kb_tasks": "list_tasks",
    "kb_remember": "remember",
    "kb_journal": "add_to_journal",
    "kb_append_to_page": "add_to_page",
    "kb_forget": "forget_memory_page",
    "kb_clear_memory": "clear_memory",
    "kb_delete_page": "delete_page",
    "kb_delete_item": "delete_item",
    "fetch_page": "read_web_page",
    "win_info": "laptop_status",
    "win_media": "laptop_media_key",
    "win_open": "open_on_laptop",
    "win_lock": "lock_laptop",
    "win_screenshot": "laptop_screenshot",
    "adb_devices": "list_phones",
    "adb_info": "phone_status",
    "adb_screenshot": "phone_screenshot",
    "adb_open_app": "open_phone_app",
    "adb_key": "phone_key",
    "adb_connect": "connect_phone",
    "adb_notifications": "phone_notifications",
    "adb_sms_read": "read_texts",
    "adb_call_log": "recent_calls",
    "adb_contacts": "find_contact",
    "fs_list": "list_files",
    "fs_read": "read_file",
    "adb_call_answer": "answer_call",
    "adb_call_end": "hang_up",
    "adb_sms_send": "send_text",
    "adb_whatsapp_send": "send_whatsapp",
    "adb_call": "make_call",
    "adb_tap": "phone_tap",
    "adb_type": "phone_type",
    "adb_shell": "phone_shell",
    "adb_install": "install_phone_app",
    "adb_uninstall": "uninstall_phone_app",
}
_INTERNAL = {v: k for k, v in MODEL_NAMES.items()}


def model_name(name: str) -> str:
    return MODEL_NAMES.get(name, name)


def internal_name(name: str) -> str:
    return _INTERNAL.get(name, name)
