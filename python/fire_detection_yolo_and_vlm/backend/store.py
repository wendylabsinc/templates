import uuid
from datetime import datetime, timezone

import loro


class LoroStore:
    def __init__(self):
        self.doc = loro.LoroDoc()
        self._conversations_list = self.doc.get_list("conversations")

    # -- Conversations --

    def create_conversation(self, title: str) -> dict:
        conversation_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).timestamp()
        m = self._conversations_list.push_container(loro.LoroMap())
        m.insert("conversationId", conversation_id)
        m.insert("title", title)
        m.insert("createdAt", now)
        m.insert("updatedAt", now)
        self.doc.commit()
        return self._conv_dict(conversation_id, title, now, now)

    def list_conversations(self) -> list[dict]:
        rows = self._conversations_list.get_deep_value()
        result = [
            self._conv_dict(
                r["conversationId"], r["title"], r["createdAt"], r["updatedAt"]
            )
            for r in rows
            if isinstance(r, dict)
        ]
        result.sort(key=lambda c: c["updatedAt"], reverse=True)
        return result

    def get_conversation(self, conversation_id: str) -> dict | None:
        for c in self.list_conversations():
            if c["conversationId"] == conversation_id:
                return c
        return None

    def update_conversation_title(self, conversation_id: str, title: str):
        rows = self._conversations_list.get_deep_value()
        for i, r in enumerate(rows):
            if isinstance(r, dict) and r.get("conversationId") == conversation_id:
                entry = self._conversations_list.get(i)
                if entry is not None and entry.is_container:
                    entry.container.insert("title", title)
                break
        self.doc.commit()

    # -- Messages --

    def send_message(self, conversation_id: str, user_id: str, body: str) -> dict:
        messages_list = self.doc.get_list(f"messages:{conversation_id}")
        chat_message_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).timestamp()
        m = messages_list.push_container(loro.LoroMap())
        m.insert("chatMessageId", chat_message_id)
        m.insert("conversationId", conversation_id)
        m.insert("createdAt", now)
        m.insert("userId", user_id)
        m.insert("body", body)
        self._update_conversation_timestamp(conversation_id, now)
        self.doc.commit()
        return self._msg_dict(chat_message_id, conversation_id, now, user_id, body)

    def add_reply(self, conversation_id: str, user_id: str, body: str) -> dict:
        messages_list = self.doc.get_list(f"messages:{conversation_id}")
        chat_message_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).timestamp()
        m = messages_list.push_container(loro.LoroMap())
        m.insert("chatMessageId", chat_message_id)
        m.insert("conversationId", conversation_id)
        m.insert("createdAt", now)
        m.insert("userId", user_id)
        m.insert("body", body)
        self._update_conversation_timestamp(conversation_id, now)
        self.doc.commit()
        return self._msg_dict(chat_message_id, conversation_id, now, user_id, body)

    def list_messages(self, conversation_id: str) -> list[dict]:
        messages_list = self.doc.get_list(f"messages:{conversation_id}")
        rows = messages_list.get_deep_value()
        return [
            self._msg_dict(
                r["chatMessageId"],
                r["conversationId"],
                r["createdAt"],
                r["userId"],
                r["body"],
            )
            for r in rows
            if isinstance(r, dict)
        ]

    # -- Sync --

    def export_snapshot(self) -> bytes:
        return bytes(self.doc.export(loro.ExportMode.Snapshot()))

    def import_snapshot(self, data: bytes):
        self.doc.import_(data)

    # -- Private --

    def _update_conversation_timestamp(self, conversation_id: str, timestamp: float):
        rows = self._conversations_list.get_deep_value()
        for i, r in enumerate(rows):
            if isinstance(r, dict) and r.get("conversationId") == conversation_id:
                entry = self._conversations_list.get(i)
                if entry is not None and entry.is_container:
                    entry.container.insert("updatedAt", timestamp)
                break

    @staticmethod
    def _conv_dict(
        conversation_id: str, title: str, created_at: float, updated_at: float
    ) -> dict:
        return {
            "conversationId": conversation_id,
            "title": title,
            "createdAt": created_at,
            "updatedAt": updated_at,
        }

    @staticmethod
    def _msg_dict(
        chat_message_id: str,
        conversation_id: str,
        created_at: float,
        user_id: str,
        body: str,
    ) -> dict:
        return {
            "chatMessageId": chat_message_id,
            "conversationId": conversation_id,
            "createdAt": created_at,
            "userId": user_id,
            "body": body,
        }
