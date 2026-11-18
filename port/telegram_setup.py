#!/usr/bin/env python3
"""
AI Agent Lens - Telegram setup helper.

Steps:
  1. Validates the bot token via getMe
  2. Discovers your chat id from recent updates (you must have sent /start)
  3. Writes port/telegram_config.json
  4. Sends a test message

Usage:
    python port/telegram_setup.py <BOT_TOKEN>
    python port/telegram_setup.py <BOT_TOKEN> <CHAT_ID>
    python port/telegram_setup.py --check
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telegram_notify as tn  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_JSON = os.path.join(HERE, "telegram_config.json")


def save(token, chat_id):
    with open(CFG_JSON, "w", encoding="utf-8") as f:
        json.dump({"bot_token": token, "chat_id": str(chat_id)}, f, indent=2)
    print(f"saved -> {CFG_JSON}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if "--check" in sys.argv:
        token, chat_id = tn.load_credentials()
        print("token:", "set" if token else "MISSING")
        print("chat_id:", chat_id or "MISSING")
        if token:
            try:
                me = tn.get_me(token)
                print("bot:", me.get("result", {}).get("username"))
            except Exception as e:
                print("token invalid:", repr(e)[:150])
        return

    if not args:
        print(__doc__)
        return

    token = args[0]
    try:
        me = tn.get_me(token)
        bot = me.get("result", {})
        print(f"OK token valid. Bot: @{bot.get('username')} ({bot.get('first_name')})")
    except Exception as e:
        print(f"Invalid token: {repr(e)[:200]}")
        return

    chat_id = args[1] if len(args) > 1 else ""
    if not chat_id:
        try:
            found = tn.discover_chat_id(token)
        except Exception as e:
            print(f"getUpdates failed: {repr(e)[:150]}")
            found = []
        if found:
            print("found chat(s):")
            for cid, title in found:
                print(f"  {cid}  {title}")
            chat_id = found[0][0]
            print(f"using: {chat_id}")
        else:
            print("\nNo chat id found yet.")
            print("Open Telegram -> search for your bot -> press START (or send /start).")
            print("Then run this script again.")
            save(token, "")
            return

    save(token, chat_id)
    ok = tn.send("Test message from AI Agent Lens.\n\n"
                 "Telegram notifications are active.\n"
                 "You will get a message when a LONG or SHORT signal fires, "
                 "plus a periodic status heartbeat.", token, chat_id)
    print("test message:", "SENT" if ok else "FAILED (see port/telegram.log)")


if __name__ == "__main__":
    main()
